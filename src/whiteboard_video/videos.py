"""Make one video and save its files and metadata in a folder."""

from __future__ import annotations

import hashlib
from pathlib import Path

import msgspec

from whiteboard_video.client import GenPixyClient, OnVideoUpdate
from whiteboard_video.models import Amount, Video, VideoRequest, VideoResult

METADATA_FILE = "video.json"
VIDEO_FILE = "video.mp4"
SUBTITLES_FILE = "subtitles.srt"
THUMBNAIL_FILE = "thumbnail.png"


class SavedVideo(msgspec.Struct, frozen=True):
    """What `video.json` holds. A folder with this file is finished."""

    id: str
    app_url: str
    cost: Amount
    request: VideoRequest
    result: VideoResult
    video_file: str
    subtitles_file: str
    thumbnail_file: str | None


def idempotency_key(request: VideoRequest, *, folder_name: str, salt: str = "") -> str:
    """The same request in the same folder always maps to the same video, so a rerun never pays twice."""
    digest = hashlib.sha256()
    digest.update(msgspec.json.encode(request))
    digest.update(f"\x00{folder_name}\x00{salt}".encode())
    return "wbv-" + digest.hexdigest()[:48]


def load_saved(folder: Path) -> SavedVideo | None:
    path = folder / METADATA_FILE
    if not path.is_file():
        return None
    return msgspec.json.decode(path.read_bytes(), type=SavedVideo)


def make_video(
    client: GenPixyClient,
    request: VideoRequest,
    folder: Path,
    *,
    salt: str = "",
    on_update: OnVideoUpdate | None = None,
) -> SavedVideo:
    """Start (or resume) the video, wait for it, and download its files into `folder`."""
    key = idempotency_key(request, folder_name=folder.name, salt=salt)
    started = client.create_video(request, idempotency_key=key)
    if on_update is not None:
        on_update(started)
    finished = started if started.result else client.wait_for_video(started.id, on_update=on_update)
    return save_files(client, finished, folder)


def save_files(client: GenPixyClient, video: Video, folder: Path) -> SavedVideo:
    if video.result is None:
        raise ValueError("Only a completed video has files.")
    files = video.result.files
    client.download(files.video, folder / VIDEO_FILE)
    client.download(files.subtitles, folder / SUBTITLES_FILE)
    thumbnail: str | None = None
    if files.thumbnail is not None:
        client.download(files.thumbnail, folder / THUMBNAIL_FILE)
        thumbnail = THUMBNAIL_FILE
    saved = SavedVideo(
        id=video.id,
        app_url=video.app_url,
        cost=video.cost,
        request=video.request,
        result=video.result,
        video_file=VIDEO_FILE,
        subtitles_file=SUBTITLES_FILE,
        thumbnail_file=thumbnail,
    )
    # Written last: its presence marks the folder as finished.
    (folder / METADATA_FILE).write_bytes(msgspec.json.format(msgspec.json.encode(saved)))
    return saved
