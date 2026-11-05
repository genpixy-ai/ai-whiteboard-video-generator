"""Optional YouTube upload. Needs the `youtube` extra: `pip install 'ai-whiteboard-video-generator[youtube]'`.

This module is the typed boundary around the untyped Google client libraries.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Callable
from pathlib import Path

import msgspec

from whiteboard_video.videos import SavedVideo

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_FILE = "youtube.json"
EDUCATION_CATEGORY_ID = "27"
MAX_TITLE_CHARACTERS = 100
MAX_DESCRIPTION_CHARACTERS = 5000
CHUNK_BYTES = 8 * 1024 * 1024

type OnUploadProgress = Callable[[float], None]


class Privacy(enum.StrEnum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class UploadedVideo(msgspec.Struct, frozen=True):
    """What `youtube.json` holds. A folder with this file is already on YouTube."""

    youtube_id: str
    url: str
    privacy: Privacy
    publish_at: str | None


class YouTubeSetupError(Exception):
    """The extra is missing or the login is missing or expired."""


class YouTubeUploadError(Exception):
    """YouTube refused the upload, for example because the daily quota is used up."""


def default_token_path() -> Path:
    if configured := os.environ.get("YOUTUBE_TOKEN_FILE"):
        return Path(configured)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "ai-whiteboard-video-generator" / "youtube-token.json"


def login(client_secret: Path, token_path: Path) -> Path:
    """Open the Google consent page once and save a refreshable login token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # optional extra
    except ImportError as error:
        raise YouTubeSetupError(_missing_extra()) from error
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return token_path


def load_uploaded(folder: Path) -> UploadedVideo | None:
    path = folder / UPLOAD_FILE
    if not path.is_file():
        return None
    return msgspec.json.decode(path.read_bytes(), type=UploadedVideo)


def description_of(saved: SavedVideo) -> str:
    text = saved.result.youtube_description.strip()
    missing = [tag for tag in saved.result.hashtags if tag not in text]
    if missing:
        text = f"{text}\n\n{' '.join(missing)}"
    return text[:MAX_DESCRIPTION_CHARACTERS]


def upload_body(saved: SavedVideo, *, privacy: Privacy, publish_at: str | None) -> dict[str, dict[str, object]]:
    title = (saved.result.youtube_title or saved.result.title)[:MAX_TITLE_CHARACTERS]
    status: dict[str, object] = {
        # YouTube publishes a scheduled video only from private.
        "privacyStatus": Privacy.PRIVATE.value if publish_at else privacy.value,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        status["publishAt"] = publish_at
    return {
        "snippet": {
            "title": title,
            "description": description_of(saved),
            "tags": [tag.lstrip("#") for tag in saved.result.hashtags],
            "categoryId": EDUCATION_CATEGORY_ID,
        },
        "status": status,
    }


def upload(
    saved: SavedVideo,
    folder: Path,
    *,
    privacy: Privacy,
    token_path: Path,
    publish_at: str | None = None,
    on_progress: OnUploadProgress | None = None,
) -> tuple[UploadedVideo, str | None]:
    """Upload the video and its thumbnail. Returns the upload and a thumbnail warning, if any."""
    try:
        from google.auth.transport.requests import Request  # optional extra
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as error:
        raise YouTubeSetupError(_missing_extra()) from error
    if not token_path.is_file():
        raise YouTubeSetupError(
            f"No YouTube login at {token_path}. Run: whiteboard-video youtube login --client-secret <file>"
        )
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not credentials.valid:
        credentials.refresh(Request())
    service = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    media = MediaFileUpload(str(folder / saved.video_file), mimetype="video/mp4", chunksize=CHUNK_BYTES, resumable=True)
    request = service.videos().insert(
        part="snippet,status",
        body=upload_body(saved, privacy=privacy, publish_at=publish_at),
        media_body=media,
    )
    response = None
    try:
        while response is None:
            progress, response = request.next_chunk()
            if progress is not None and on_progress is not None:
                on_progress(float(progress.progress()))
    except HttpError as error:
        raise YouTubeUploadError(f"YouTube refused the upload (HTTP {error.status_code}): {error.reason}") from error
    youtube_id = str(response["id"])

    warning: str | None = None
    if saved.thumbnail_file is not None:
        try:
            service.thumbnails().set(
                videoId=youtube_id,
                media_body=MediaFileUpload(str(folder / saved.thumbnail_file), mimetype="image/png"),
            ).execute()
        except HttpError as error:
            # Custom thumbnails need a verified channel; the video itself is uploaded.
            warning = (
                f"YouTube did not accept the thumbnail (HTTP {error.status_code}). "
                "Verify the channel to use custom thumbnails."
            )

    uploaded = UploadedVideo(
        youtube_id=youtube_id,
        url=f"https://www.youtube.com/watch?v={youtube_id}",
        privacy=Privacy.PRIVATE if publish_at else privacy,
        publish_at=publish_at,
    )
    (folder / UPLOAD_FILE).write_bytes(msgspec.json.format(msgspec.json.encode(uploaded)))
    return uploaded, warning


def _missing_extra() -> str:
    return (
        "YouTube upload needs extra packages. Install with: "
        "uv tool install 'ai-whiteboard-video-generator[youtube] @ "
        "git+https://github.com/genpixy-ai/ai-whiteboard-video-generator'"
    )
