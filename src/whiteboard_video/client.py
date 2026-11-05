"""Small typed client for the GenPixy video API."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import msgspec

from whiteboard_video import __version__
from whiteboard_video.models import (
    Account,
    ErrorEnvelope,
    Options,
    Video,
    VideoRequest,
)

DEFAULT_BASE_URL = "https://www.genpixy.com/api/v1"
DEFAULT_POLL_INTERVAL_S = 10.0
MAX_ATTEMPTS = 5
MAX_BACKOFF_S = 30.0
TRY_AGAIN_WAIT_S = 30.0

type Sleep = Callable[[float], None]
type OnVideoUpdate = Callable[[Video], None]


class ApiError(Exception):
    """An error answer from the API. `message` is written for people."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        request_id: str | None = None,
        retry_after_s: float | None = None,
        top_up_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after_s = retry_after_s
        self.top_up_url = top_up_url

    @property
    def is_transient(self) -> bool:
        # 5xx answers and rate limits clear on their own; everything else needs a change.
        return self.status >= 500 or self.code == "RATE_LIMITED"


class VideoFailedError(Exception):
    """The video ended as failed or canceled. Nothing was charged."""

    def __init__(self, video: Video) -> None:
        message = video.error.message if video.error else f"The video ended as {video.status}."
        super().__init__(message)
        self.video = video


def _error_from(response: httpx.Response) -> ApiError:
    try:
        body = msgspec.json.decode(response.content, type=ErrorEnvelope).error
    except msgspec.DecodeError:
        return ApiError(
            status=response.status_code,
            code="UNEXPECTED_RESPONSE",
            message=f"The API answered HTTP {response.status_code} without an error body.",
        )
    retry_after = body.retry_after_seconds
    if retry_after is None and (header := response.headers.get("retry-after")):
        try:
            retry_after = float(header)
        except ValueError:
            retry_after = None
    return ApiError(
        status=response.status_code,
        code=body.code,
        message=body.message,
        request_id=body.request_id,
        retry_after_s=retry_after,
        top_up_url=body.top_up_url,
    )


def _backoff_s(attempt: int, error: ApiError | None) -> float:
    if error is not None and error.retry_after_s is not None:
        return error.retry_after_s
    if error is not None and error.code == "TRY_AGAIN":
        return TRY_AGAIN_WAIT_S
    return min(MAX_BACKOFF_S, 2.0 * 2**attempt)


class GenPixyClient:
    """Create videos, follow them until they finish, and download the files."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.Client | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        if not api_key.startswith("gpx_"):
            raise ValueError("A GenPixy API key starts with 'gpx_'. Create one at genpixy.com.")
        self._base_url = httpx.URL(base_url.rstrip("/") + "/")
        self._sleep = sleep
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": f"ai-whiteboard-video-generator/{__version__}",
        }

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def account(self) -> Account:
        return msgspec.json.decode(self._send("GET", "account").content, type=Account)

    def options(self) -> Options:
        return msgspec.json.decode(self._send("GET", "options").content, type=Options)

    def create_video(self, request: VideoRequest, *, idempotency_key: str) -> Video:
        """Start a video. The same key and request never start or charge a second video."""
        request.validate()
        response = self._send(
            "POST",
            "videos",
            content=msgspec.json.encode(request),
            headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        return msgspec.json.decode(response.content, type=Video)

    def get_video(self, video_id: str) -> Video:
        return msgspec.json.decode(self._send("GET", f"videos/{video_id}").content, type=Video)

    def wait_for_video(
        self,
        video_id: str,
        *,
        on_update: OnVideoUpdate | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> Video:
        """Check the video until it is completed. Raise VideoFailedError when it fails."""
        while True:
            video = self.get_video(video_id)
            if on_update is not None:
                on_update(video)
            if video.status.is_final:
                if video.result is None:
                    raise VideoFailedError(video)
                return video
            self._sleep(poll_interval_s)

    def download(self, url: str, destination: Path) -> Path:
        """Download one file link of a completed video. Writes atomically."""
        target = httpx.URL(url)
        if target.host != self._base_url.host:
            # The key must never go to another site.
            raise ValueError(f"Refusing to send the API key to {target.host}.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self._http.stream("GET", target, headers=self._headers, follow_redirects=True) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise _error_from(response)
                    with partial.open("wb") as file:
                        for chunk in response.iter_bytes():
                            file.write(chunk)
                partial.replace(destination)
                return destination
            except (httpx.TransportError, ApiError) as error:
                api_error = error if isinstance(error, ApiError) else None
                if (api_error and not api_error.is_transient) or attempt == MAX_ATTEMPTS - 1:
                    partial.unlink(missing_ok=True)
                    raise
                self._sleep(_backoff_s(attempt, api_error))
        raise AssertionError("unreachable")

    def _send(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request. Retries network errors, 5xx answers, and rate limits.

        Retrying POST /videos is safe because it always carries an Idempotency-Key.
        """
        url = self._base_url.join(path)
        all_headers = {**self._headers, **(headers or {})}
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._http.request(method, url, content=content, headers=all_headers)
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                self._sleep(_backoff_s(attempt, None))
                continue
            if response.status_code < 400:
                return response
            error = _error_from(response)
            if not error.is_transient or attempt == MAX_ATTEMPTS - 1:
                raise error
            self._sleep(_backoff_s(attempt, error))
        raise AssertionError("unreachable")
