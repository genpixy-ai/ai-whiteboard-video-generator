"""`whiteboard-video` command line tool."""

from __future__ import annotations

import enum
import sys
import time
from pathlib import Path
from typing import Annotated

import msgspec
import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn
from rich.table import Table

from whiteboard_video.client import DEFAULT_BASE_URL, ApiError, GenPixyClient, VideoFailedError
from whiteboard_video.models import (
    VIDEO_SECONDS,
    Language,
    Orientation,
    Resolution,
    Style,
    Video,
    VideoRequest,
    VideoStatus,
    Voice,
)
from whiteboard_video.topics import Topic, TopicsFileError, folder_name_for, read_topics
from whiteboard_video.videos import SavedVideo, load_saved, make_video
from whiteboard_video.youtube import (
    Privacy,
    YouTubeSetupError,
    YouTubeUploadError,
    default_token_path,
    load_uploaded,
    login,
    upload,
)

KEY_URL = "https://www.genpixy.com/account/api-keys"
BUSY_WAIT_S = 30.0
BUSY_MAX_WAIT_S = 30 * 60.0

err = Console(stderr=True)
out = Console(highlight=False)

app = typer.Typer(
    name="whiteboard-video",
    help="AI whiteboard video generator: turn a topic or a script into a narrated explainer video.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
youtube_app = typer.Typer(help="Upload finished videos to YouTube.", no_args_is_help=True)
app.add_typer(youtube_app, name="youtube")


class Outcome(enum.StrEnum):
    MADE = "made"
    ALREADY_MADE = "already_made"
    FAILED = "failed"
    SKIPPED = "skipped"


class TopicReport(msgspec.Struct, frozen=True):
    folder: str
    prompt: str
    outcome: Outcome
    message: str | None = None
    video: SavedVideo | None = None
    youtube_url: str | None = None


ApiKey = Annotated[
    str,
    typer.Option(
        "--api-key",
        envvar="GENPIXY_API_KEY",
        help=f"GenPixy API key. Create one at {KEY_URL}.",
        show_default=False,
    ),
]
ApiUrl = Annotated[str, typer.Option("--api-url", envvar="GENPIXY_API_URL", hidden=True)]
Seconds = Annotated[int, typer.Option(help="Video length in seconds: 30, 60, 90, 120, 180, 300, 480, or 600.")]
Portrait = Annotated[bool, typer.Option("--portrait", help="Vertical video for Shorts, TikTok, and Reels.")]
ResolutionOpt = Annotated[Resolution, typer.Option(help="720p or 1080p.")]
StyleOpt = Annotated[Style, typer.Option(help="whiteboard, animate (cartoon), or flat (color board).")]
LanguageOpt = Annotated[Language, typer.Option(help="auto follows the prompt language.")]
VoiceOpt = Annotated[Voice, typer.Option(help="Narrator voice.")]
Subtitles = Annotated[bool, typer.Option("--subtitles/--no-subtitles", help="Word-by-word subtitles.")]
Music = Annotated[bool, typer.Option("--music/--no-music", help="Background music.")]
BigBoard = Annotated[bool, typer.Option("--big-board", help="All scenes on one board, zoom-out recap.")]
ZoomDive = Annotated[bool, typer.Option("--zoom-dive", help="Camera flies into icons between scenes.")]
OutDir = Annotated[Path, typer.Option("--out", "-o", help="Folder for the videos.")]
Fresh = Annotated[bool, typer.Option("--fresh", help="Make a new video even if this topic was made before.")]
YouTube = Annotated[
    Privacy | None,
    typer.Option("--youtube", help="Also upload to YouTube as private, unlisted, or public."),
]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before spending balance.")]
JsonOut = Annotated[bool, typer.Option("--json", help="Print results as JSON on stdout.")]


def _request(
    prompt: str,
    *,
    seconds: int,
    portrait: bool,
    resolution: Resolution,
    style: Style,
    language: Language,
    voice: Voice,
    subtitles: bool,
    music: bool,
    big_board: bool,
    zoom_dive: bool,
) -> VideoRequest:
    request = VideoRequest(
        prompt=prompt,
        seconds=seconds,
        orientation=Orientation.PORTRAIT if portrait else Orientation.LANDSCAPE,
        resolution=resolution,
        style=style,
        language=language,
        voice=voice,
        subtitles=subtitles,
        music=music,
        big_board=big_board,
        zoom_dive=zoom_dive,
    )
    if seconds not in VIDEO_SECONDS:
        raise typer.BadParameter("use " + ", ".join(str(value) for value in VIDEO_SECONDS), param_hint="--seconds")
    return request


def _client(api_key: str, api_url: str) -> GenPixyClient:
    if not api_key:
        err.print(f"[red]✗[/] No API key. Create one at {KEY_URL}, then run: export GENPIXY_API_KEY=gpx_…")
        raise typer.Exit(1)
    try:
        return GenPixyClient(api_key, base_url=api_url)
    except ValueError as error:
        err.print(f"[red]✗[/] {error}")
        raise typer.Exit(1) from error


def _fail(error: ApiError) -> typer.Exit:
    err.print(f"[red]✗[/] {error.message}")
    if error.top_up_url:
        err.print(f"  Top up: {error.top_up_url}")
    if error.request_id:
        err.print(f"  [dim]Request ID for support: {error.request_id}[/]")
    return typer.Exit(1)


def _confirm_cost(client: GenPixyClient, requests: list[VideoRequest], *, yes: bool) -> None:
    options = client.options()
    account = client.account()
    costs = [options.cost_of(seconds=r.seconds, resolution=r.resolution) for r in requests]
    known = [cost for cost in costs if cost is not None]
    total_units = sum(cost.units for cost in known)
    count = len(requests)
    label = "1 video" if count == 1 else f"{count} videos"
    each = known[0].display if len(known) == 1 else ", ".join(sorted({c.display for c in known}))
    err.print(f"Making [bold]{label}[/]. Cost per video: {each}. Balance: {account.balance.display}.")
    if total_units > account.balance.units:
        err.print(f"[yellow]⚠[/] The balance may not cover all videos. Top up: {account.top_up_url}")
    if yes:
        return
    if not sys.stdin.isatty():
        err.print("[red]✗[/] Add --yes to start videos without a prompt.")
        raise typer.Exit(1)
    if not typer.confirm("Start?", default=True, err=True):
        err.print("Canceled. Nothing was started.")
        raise typer.Exit(0)


def _status_text(video: Video) -> str:
    match video.status:
        case VideoStatus.QUEUED:
            position = video.queue_position
            return f"waiting in line (#{position})" if position else "waiting in line"
        case VideoStatus.IN_PROGRESS:
            return "making"
        case VideoStatus.COMPLETED:
            return "saving files"
        case VideoStatus.FAILED | VideoStatus.CANCELED:
            return str(video.status)


def _make_with_retry(
    client: GenPixyClient,
    topic: Topic,
    folder: Path,
    *,
    salt: str,
    progress: Progress,
    task: TaskID,
) -> SavedVideo:
    """Make one video. Waits while the account already has the most videos in progress."""
    waited = 0.0

    def on_update(video: Video) -> None:
        progress.update(task, completed=video.progress, status=_status_text(video))

    while True:
        try:
            return make_video(client, topic.request, folder, salt=salt, on_update=on_update)
        except ApiError as error:
            if error.code != "TOO_MANY_ACTIVE_VIDEOS" or waited >= BUSY_MAX_WAIT_S:
                raise
            progress.update(task, status="waiting for a free slot")
            time.sleep(BUSY_WAIT_S)
            waited += BUSY_WAIT_S


def _upload_one(saved: SavedVideo, folder: Path, privacy: Privacy, progress: Progress, task: TaskID) -> str | None:
    if existing := load_uploaded(folder):
        return existing.url
    if not (folder / saved.video_file).is_file():
        # For example a scheduled run that kept only video.json from an earlier day.
        progress.console.print(f"[yellow]⚠[/] {saved.result.title}: no video file in {folder}, not uploaded.")
        return None
    progress.update(task, completed=0, status="uploading to YouTube")

    def on_progress(share: float) -> None:
        progress.update(task, completed=share * 100)

    uploaded, warning = upload(saved, folder, privacy=privacy, token_path=default_token_path(), on_progress=on_progress)
    if warning:
        progress.console.print(f"[yellow]⚠[/] {warning}")
    return uploaded.url


def _process(
    client: GenPixyClient,
    topic: Topic,
    out_dir: Path,
    *,
    salt: str,
    youtube: Privacy | None,
    progress: Progress,
) -> TopicReport:
    folder = out_dir / topic.folder_name
    title = topic.request.prompt.splitlines()[0][:48]
    task = progress.add_task(title, total=100, status="starting")
    saved = load_saved(folder)
    outcome = Outcome.ALREADY_MADE
    try:
        if saved is None:
            saved = _make_with_retry(client, topic, folder, salt=salt, progress=progress, task=task)
            outcome = Outcome.MADE
        youtube_url = _upload_one(saved, folder, youtube, progress, task) if youtube else None
    except VideoFailedError as error:
        progress.update(task, status="[red]failed[/]")
        return TopicReport(str(folder), topic.request.prompt, Outcome.FAILED, message=str(error))
    except ApiError as error:
        if error.code in ("INSUFFICIENT_BALANCE", "UNAUTHORIZED", "ACCOUNT_SUSPENDED"):
            raise
        progress.update(task, status="[red]failed[/]")
        return TopicReport(str(folder), topic.request.prompt, Outcome.FAILED, message=error.message)
    progress.update(task, completed=100, status="[green]done[/]")
    return TopicReport(str(folder), topic.request.prompt, outcome, video=saved, youtube_url=youtube_url)


def _summary(reports: list[TopicReport]) -> None:
    table = Table(title="Results", title_justify="left", show_lines=False)
    table.add_column("Video")
    table.add_column("Result")
    table.add_column("Length", justify="right")
    table.add_column("Folder / link")
    for report in reports:
        video = report.video
        title = video.result.title if video else report.prompt.splitlines()[0][:60]
        length = f"{video.result.duration_seconds:.0f} s" if video else "-"
        result = {
            Outcome.MADE: "[green]✓ made[/]",
            Outcome.ALREADY_MADE: "[green]✓ already made[/]",
            Outcome.FAILED: f"[red]✗ {report.message or 'failed'}[/]",
            Outcome.SKIPPED: "[dim]skipped[/]",
        }[report.outcome]
        where = report.folder + (f"\n{report.youtube_url}" if report.youtube_url else "")
        table.add_row(title, result, length, where)
    err.print(table)


def _run(
    client: GenPixyClient,
    topics: list[Topic],
    out_dir: Path,
    *,
    fresh: bool,
    youtube: Privacy | None,
    yes: bool,
    json_output: bool,
) -> None:
    to_make = [t for t in topics if load_saved(out_dir / t.folder_name) is None]
    if to_make:
        _confirm_cost(client, [t.request for t in to_make], yes=yes)
    salt = str(time.time_ns()) if fresh else ""
    reports: list[TopicReport] = []
    columns = (
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TextColumn("[dim]{task.fields[status]}[/]"),
    )
    try:
        with Progress(*columns, console=err, transient=False) as progress:
            for topic in topics:
                reports.append(_process(client, topic, out_dir, salt=salt, youtube=youtube, progress=progress))
    except KeyboardInterrupt:
        err.print(
            "\nStopped. Started videos keep going on GenPixy. "
            "Run the same command again to resume; you do not pay twice."
        )
        raise typer.Exit(130) from None
    except ApiError as error:
        raise _fail(error) from error
    except (YouTubeSetupError, YouTubeUploadError) as error:
        err.print(f"[red]✗[/] {error}")
        raise typer.Exit(1) from error
    _summary(reports)
    if json_output:
        print(msgspec.json.encode(reports).decode())
    if any(report.outcome is Outcome.FAILED for report in reports):
        raise typer.Exit(1)


@app.command()
def make(
    prompt: Annotated[str, typer.Argument(help="A topic, notes, or a full script (3 to 5,000 characters).")],
    api_key: ApiKey = "",
    api_url: ApiUrl = DEFAULT_BASE_URL,
    seconds: Seconds = 60,
    portrait: Portrait = False,
    resolution: ResolutionOpt = Resolution.P720,
    style: StyleOpt = Style.WHITEBOARD,
    language: LanguageOpt = Language.AUTO,
    voice: VoiceOpt = Voice.MALE,
    subtitles: Subtitles = True,
    music: Music = True,
    big_board: BigBoard = False,
    zoom_dive: ZoomDive = False,
    out_dir: OutDir = Path("videos"),
    fresh: Fresh = False,
    youtube: YouTube = None,
    yes: Yes = False,
    json_output: JsonOut = False,
) -> None:
    """Make one video from a topic or a script."""
    request = _request(
        prompt, seconds=seconds, portrait=portrait, resolution=resolution, style=style,
        language=language, voice=voice, subtitles=subtitles, music=music,
        big_board=big_board, zoom_dive=zoom_dive,
    )  # fmt: skip
    try:
        request.validate()
    except ValueError as error:
        raise typer.BadParameter(str(error), param_hint="PROMPT") from error
    topic = Topic(folder_name=folder_name_for(request.prompt, None), request=request)
    with _client(api_key, api_url) as client:
        _run(client, [topic], out_dir, fresh=fresh, youtube=youtube, yes=yes, json_output=json_output)


@app.command()
def batch(
    topics_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="CSV with a 'prompt' column, or a text file with one topic per line.",
        ),
    ],
    api_key: ApiKey = "",
    api_url: ApiUrl = DEFAULT_BASE_URL,
    seconds: Seconds = 60,
    portrait: Portrait = False,
    resolution: ResolutionOpt = Resolution.P720,
    style: StyleOpt = Style.WHITEBOARD,
    language: LanguageOpt = Language.AUTO,
    voice: VoiceOpt = Voice.MALE,
    subtitles: Subtitles = True,
    music: Music = True,
    big_board: BigBoard = False,
    zoom_dive: ZoomDive = False,
    out_dir: OutDir = Path("videos"),
    limit: Annotated[int | None, typer.Option(min=1, help="Make at most this many new videos.")] = None,
    fresh: Fresh = False,
    youtube: YouTube = None,
    yes: Yes = False,
    json_output: JsonOut = False,
) -> None:
    """Make a video for every topic in a file. Finished topics are skipped, so reruns resume."""
    defaults = _request(
        "placeholder", seconds=seconds, portrait=portrait, resolution=resolution, style=style,
        language=language, voice=voice, subtitles=subtitles, music=music,
        big_board=big_board, zoom_dive=zoom_dive,
    )  # fmt: skip
    try:
        topics = read_topics(topics_file, defaults)
    except TopicsFileError as error:
        err.print(f"[red]✗[/] {error.path} has problems:")
        for problem in error.problems:
            err.print(f"  • {problem}")
        raise typer.Exit(1) from error
    if limit is not None:
        unmade = [t for t in topics if load_saved(out_dir / t.folder_name) is None]
        topics = unmade[:limit]
    if not topics:
        err.print(f"[green]✓[/] Every topic in {topics_file} is already made in {out_dir}/.")
        return
    with _client(api_key, api_url) as client:
        _run(client, topics, out_dir, fresh=fresh, youtube=youtube, yes=yes, json_output=json_output)


@app.command()
def account(api_key: ApiKey = "", api_url: ApiUrl = DEFAULT_BASE_URL, json_output: JsonOut = False) -> None:
    """Show the balance and the videos in progress."""
    with _client(api_key, api_url) as client:
        try:
            info = client.account()
        except ApiError as error:
            raise _fail(error) from error
    if json_output:
        print(msgspec.json.encode(info).decode())
        return
    table = Table(show_header=False, box=None)
    table.add_row("Account", info.email)
    table.add_row("Balance", info.balance.display)
    table.add_row("Reserved", info.reserved.display)
    table.add_row("In progress", f"{info.active_videos} of {info.max_active_videos}")
    table.add_row("Top up", info.top_up_url)
    out.print(table)


@app.command()
def prices(api_key: ApiKey = "", api_url: ApiUrl = DEFAULT_BASE_URL, json_output: JsonOut = False) -> None:
    """Show the cost of each video length and resolution for your account."""
    with _client(api_key, api_url) as client:
        try:
            options = client.options()
        except ApiError as error:
            raise _fail(error) from error
    if json_output:
        print(msgspec.json.encode(options.costs).decode())
        return
    listed = {item.resolution for item in options.costs}
    resolutions = [resolution for resolution in Resolution if resolution in listed]
    table = Table(title="Cost per video", title_justify="left")
    table.add_column("Length", justify="right")
    for resolution in resolutions:
        table.add_column(str(resolution), justify="right")
    for seconds in sorted({item.seconds for item in options.costs}):
        cells = [options.cost_of(seconds=seconds, resolution=r) for r in resolutions]
        table.add_row(f"{seconds} s", *(cell.display if cell else "-" for cell in cells))
    out.print(table)


@youtube_app.command("login")
def youtube_login(
    client_secret: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, help="OAuth client file (Desktop app) from Google Cloud."),
    ],
) -> None:
    """Sign in to YouTube once. Saves a token that later uploads reuse."""
    try:
        path = login(client_secret, default_token_path())
    except YouTubeSetupError as error:
        err.print(f"[red]✗[/] {error}")
        raise typer.Exit(1) from error
    err.print(f"[green]✓[/] Signed in. Token saved to {path}")


@youtube_app.command("upload")
def youtube_upload(
    folder: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, help="A video folder, or the folder of all videos."),
    ],
    privacy: Annotated[Privacy, typer.Option(help="private, unlisted, or public.")] = Privacy.PRIVATE,
    publish_at: Annotated[
        str | None,
        typer.Option(help="Schedule, e.g. 2026-12-01T09:00:00Z (uploads as private until then)."),
    ] = None,
) -> None:
    """Upload made videos with their YouTube title, description, tags, and thumbnail."""
    folders = [folder] if load_saved(folder) else sorted(p for p in folder.iterdir() if load_saved(p))
    if not folders:
        err.print(f'No made videos in {folder}. Make one with: whiteboard-video make "<topic>"')
        raise typer.Exit(1)
    for item in folders:
        saved = load_saved(item)
        if saved is None:
            continue
        if existing := load_uploaded(item):
            err.print(f"[dim]• {saved.result.title}: already on YouTube ({existing.url})[/]")
            continue
        if not (item / saved.video_file).is_file():
            err.print(f"[dim]• {saved.result.title}: no video file in {item}, skipped[/]")
            continue
        try:
            uploaded, warning = upload(
                saved, item, privacy=privacy, publish_at=publish_at, token_path=default_token_path()
            )
        except (YouTubeSetupError, YouTubeUploadError) as error:
            err.print(f"[red]✗[/] {error}")
            raise typer.Exit(1) from error
        if warning:
            err.print(f"[yellow]⚠[/] {warning}")
        err.print(f"[green]✓[/] {saved.result.title} → {uploaded.url} ({uploaded.privacy})")


def main() -> None:
    app()
