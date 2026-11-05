"""Typed models for the GenPixy video API (https://www.genpixy.com/api/docs).

Responses may gain new fields later; decoding ignores unknown fields.
"""

from __future__ import annotations

import enum

import msgspec


class VideoStatus(enum.StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_final(self) -> bool:
        return self in (VideoStatus.COMPLETED, VideoStatus.FAILED, VideoStatus.CANCELED)


class Orientation(enum.StrEnum):
    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"


class Resolution(enum.StrEnum):
    P720 = "720p"
    P1080 = "1080p"


class Style(enum.StrEnum):
    WHITEBOARD = "whiteboard"
    ANIMATE = "animate"
    FLAT = "flat"


class Language(enum.StrEnum):
    AUTO = "auto"
    EN = "en"
    ID = "id"


class Voice(enum.StrEnum):
    MALE = "male"
    FEMALE = "female"


class FileVariant(enum.StrEnum):
    VIDEO = "video"
    THUMBNAIL = "thumbnail"
    SUBTITLES = "subtitles"


VIDEO_SECONDS: tuple[int, ...] = (30, 60, 90, 120, 180, 300, 480, 600)
MAX_PROMPT_CHARACTERS = 5000


class Amount(msgspec.Struct, frozen=True):
    """An amount of account balance. Compare `units` in code; show `display` to people."""

    units: int
    display: str


class VideoRequest(msgspec.Struct, frozen=True):
    """Body of `POST /api/v1/videos`."""

    prompt: str
    seconds: int = 60
    orientation: Orientation = Orientation.LANDSCAPE
    resolution: Resolution = Resolution.P720
    style: Style = Style.WHITEBOARD
    language: Language = Language.AUTO
    voice: Voice = Voice.MALE
    subtitles: bool = True
    music: bool = True
    big_board: bool = False
    zoom_dive: bool = False

    def validate(self) -> None:
        """Raise ValueError with a readable message when a field is out of range."""
        length = len(self.prompt.strip())
        if length < 3 or length > MAX_PROMPT_CHARACTERS:
            raise ValueError(f"The prompt must have 3 to {MAX_PROMPT_CHARACTERS:,} characters (it has {length:,}).")
        if self.seconds not in VIDEO_SECONDS:
            allowed = ", ".join(str(value) for value in VIDEO_SECONDS)
            raise ValueError(f"Video length must be one of {allowed} seconds (got {self.seconds}).")


class VideoFiles(msgspec.Struct, frozen=True):
    video: str
    thumbnail: str | None
    subtitles: str


class VideoResult(msgspec.Struct, frozen=True):
    title: str
    duration_seconds: float
    width: int
    height: int
    size_bytes: int
    youtube_title: str
    youtube_description: str
    hashtags: list[str]
    files: VideoFiles


class VideoError(msgspec.Struct, frozen=True):
    code: str
    message: str


class Video(msgspec.Struct, frozen=True):
    id: str
    status: VideoStatus
    progress: int
    queue_position: int | None
    created_at: str
    cost: Amount
    request: VideoRequest
    result: VideoResult | None
    error: VideoError | None
    app_url: str
    started_at: str | None = None
    completed_at: str | None = None
    expires_at: str | None = None


class Account(msgspec.Struct, frozen=True):
    email: str
    balance: Amount
    reserved: Amount
    active_videos: int
    max_active_videos: int
    top_up_url: str


class VideoCost(msgspec.Struct, frozen=True):
    seconds: int
    resolution: Resolution
    cost: Amount


class Options(msgspec.Struct, frozen=True):
    seconds: list[int]
    orientations: list[str]
    resolutions: list[str]
    styles: list[str]
    languages: list[str]
    voices: list[str]
    costs: list[VideoCost]
    max_prompt_characters: int
    max_active_videos: int
    poll_interval_seconds: int

    def cost_of(self, *, seconds: int, resolution: Resolution) -> Amount | None:
        for item in self.costs:
            if item.seconds == seconds and item.resolution == resolution:
                return item.cost
        return None


class ErrorBody(msgspec.Struct, frozen=True):
    code: str
    message: str
    request_id: str | None = None
    retry_after_seconds: float | None = None
    top_up_url: str | None = None


class ErrorEnvelope(msgspec.Struct, frozen=True):
    error: ErrorBody
