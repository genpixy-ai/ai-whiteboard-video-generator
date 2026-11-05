from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from whiteboard_video.client import ApiError, GenPixyClient, VideoFailedError
from whiteboard_video.models import VideoRequest, VideoStatus
from whiteboard_video.videos import idempotency_key, load_saved, make_video

BASE = "https://www.genpixy.com/api/v1"
KEY = "gpx_" + "a" * 43


def video_json(status: str = "queued", *, progress: int = 0, video_id: str = "vid-1") -> dict[str, object]:
    done = status == "completed"
    files = {
        "video": f"{BASE}/videos/{video_id}/content?variant=video",
        "thumbnail": f"{BASE}/videos/{video_id}/content?variant=thumbnail",
        "subtitles": f"{BASE}/videos/{video_id}/content?variant=subtitles",
    }
    return {
        "id": video_id,
        "object": "video",
        "status": status,
        "progress": progress,
        "queue_position": None,
        "created_at": "2026-11-05T00:00:00.000Z",
        "cost": {"units": 16, "display": "16 coins"},
        "request": {
            "prompt": "How black holes form",
            "seconds": 60,
            "orientation": "landscape",
            "resolution": "720p",
            "style": "whiteboard",
            "language": "auto",
            "voice": "male",
            "subtitles": True,
            "music": True,
            "big_board": False,
            "zoom_dive": False,
        },
        "result": {
            "title": "How Black Holes Form",
            "duration_seconds": 61.4,
            "width": 1280,
            "height": 720,
            "size_bytes": 5,
            "youtube_title": "How Do Black Holes Form?",
            "youtube_description": "A short explainer.",
            "hashtags": ["#space"],
            "files": files,
        }
        if done
        else None,
        "error": {"code": "VIDEO_FAILED", "message": "We could not finish this video."} if status == "failed" else None,
        "app_url": f"https://www.genpixy.com/app/drawgen/{video_id}",
        "future_field": "ignored",
    }


def error_json(code: str, message: str = "Nope.") -> dict[str, object]:
    return {"error": {"code": code, "message": message, "request_id": "req-1"}}


def make_client(handler: httpx.MockTransport) -> tuple[GenPixyClient, list[float]]:
    sleeps: list[float] = []
    client = GenPixyClient(KEY, http=httpx.Client(transport=handler), sleep=sleeps.append)
    return client, sleeps


def test_create_retries_try_again_with_the_same_idempotency_key() -> None:
    seen_keys: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers.get("Idempotency-Key"))
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        if len(seen_keys) == 1:
            return httpx.Response(503, json=error_json("TRY_AGAIN"))
        return httpx.Response(201, json=video_json())

    client, sleeps = make_client(httpx.MockTransport(handle))
    video = client.create_video(VideoRequest(prompt="How black holes form"), idempotency_key="k-1")

    assert video.status is VideoStatus.QUEUED
    assert seen_keys == ["k-1", "k-1"]
    assert sleeps == [30.0]


def test_client_error_is_not_retried_and_keeps_details() -> None:
    calls = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(402, json=error_json("INSUFFICIENT_BALANCE", "Top up first."))

    client, _ = make_client(httpx.MockTransport(handle))
    with pytest.raises(ApiError) as caught:
        client.account()

    assert calls == 1
    assert caught.value.code == "INSUFFICIENT_BALANCE"
    assert caught.value.message == "Top up first."
    assert caught.value.request_id == "req-1"


def test_rate_limit_waits_retry_after_header() -> None:
    answers = iter(
        [
            httpx.Response(429, json=error_json("RATE_LIMITED"), headers={"retry-after": "7"}),
            httpx.Response(200, json=video_json("in_progress", progress=40)),
        ]
    )
    client, sleeps = make_client(httpx.MockTransport(lambda _: next(answers)))

    assert client.get_video("vid-1").progress == 40
    assert sleeps == [7.0]


def test_wait_raises_when_the_video_fails() -> None:
    answers = iter(
        [httpx.Response(200, json=video_json("in_progress")), httpx.Response(200, json=video_json("failed"))]
    )
    client, _ = make_client(httpx.MockTransport(lambda _: next(answers)))

    with pytest.raises(VideoFailedError, match="could not finish"):
        client.wait_for_video("vid-1")


def test_download_refuses_to_send_the_key_to_another_host(tmp_path: Path) -> None:
    client, _ = make_client(httpx.MockTransport(lambda _: httpx.Response(200, content=b"x")))

    with pytest.raises(ValueError, match=r"evil\.example"):
        client.download("https://evil.example/file.mp4", tmp_path / "video.mp4")


def test_rejects_a_key_with_the_wrong_prefix() -> None:
    with pytest.raises(ValueError, match="gpx_"):
        GenPixyClient("sk-123")


def test_make_video_saves_files_then_metadata_and_resumes(tmp_path: Path) -> None:
    statuses = iter(["queued", "in_progress", "completed"])
    posts: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.headers.get("Idempotency-Key"))
            body = json.loads(request.content)
            assert body["prompt"] == "How black holes form"
            return httpx.Response(201, json=video_json())
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=request.url.params["variant"].encode())
        return httpx.Response(200, json=video_json(next(statuses)))

    client, _ = make_client(httpx.MockTransport(handle))
    folder = tmp_path / "black-holes"
    request = VideoRequest(prompt="How black holes form")
    updates: list[VideoStatus] = []

    saved = make_video(client, request, folder, on_update=lambda video: updates.append(video.status))

    assert updates == [VideoStatus.QUEUED, VideoStatus.QUEUED, VideoStatus.IN_PROGRESS, VideoStatus.COMPLETED]
    assert (folder / "video.mp4").read_bytes() == b"video"
    assert (folder / "subtitles.srt").read_bytes() == b"subtitles"
    assert (folder / "thumbnail.png").read_bytes() == b"thumbnail"
    assert not list(folder.glob("*.part"))
    assert load_saved(folder) == saved
    assert posts == [idempotency_key(request, folder_name="black-holes")]


def test_idempotency_key_is_stable_and_changes_with_salt() -> None:
    request = VideoRequest(prompt="How black holes form")
    first = idempotency_key(request, folder_name="a")

    assert first == idempotency_key(request, folder_name="a")
    assert first != idempotency_key(request, folder_name="b")
    assert first != idempotency_key(request, folder_name="a", salt="1")
    assert len(first) <= 255
