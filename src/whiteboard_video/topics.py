"""Read a topics file: a CSV with a `prompt` column, or plain text with one topic per line."""

from __future__ import annotations

import csv
import enum
import hashlib
import re
from pathlib import Path

import msgspec

from whiteboard_video.models import (
    Language,
    Orientation,
    Resolution,
    Style,
    VideoRequest,
    Voice,
)

_ENUM_COLUMNS: dict[str, type[enum.StrEnum]] = {
    "orientation": Orientation,
    "resolution": Resolution,
    "style": Style,
    "language": Language,
    "voice": Voice,
}
_BOOL_COLUMNS = ("subtitles", "music", "big_board", "zoom_dive")
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
KNOWN_COLUMNS = frozenset({"id", "prompt", "seconds", *_ENUM_COLUMNS, *_BOOL_COLUMNS})


class Topic(msgspec.Struct, frozen=True):
    folder_name: str
    request: VideoRequest


class TopicsFileError(Exception):
    def __init__(self, path: Path, problems: list[str]) -> None:
        super().__init__(f"{path}: " + "; ".join(problems))
        self.path = path
        self.problems = problems


def folder_name_for(prompt: str, row_id: str | None) -> str:
    if row_id:
        return _slug(row_id) or hashlib.sha256(row_id.encode()).hexdigest()[:12]
    words = _slug(prompt)[:48].rstrip("-") or "video"
    return f"{words}-{hashlib.sha256(prompt.encode()).hexdigest()[:8]}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _parse_bool(value: str, column: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"{column} must be yes or no (got {value!r})")


def _row_request(row: dict[str, str], defaults: VideoRequest) -> VideoRequest:
    changes: dict[str, str | int | bool | enum.StrEnum] = {"prompt": row["prompt"].strip()}
    if seconds := row.get("seconds", "").strip():
        if not seconds.isdigit():
            raise ValueError(f"seconds must be a whole number (got {seconds!r})")
        changes["seconds"] = int(seconds)
    for column, enum_type in _ENUM_COLUMNS.items():
        if value := row.get(column, "").strip():
            try:
                changes[column] = enum_type(value.lower())
            except ValueError:
                allowed = ", ".join(member.value for member in enum_type)
                raise ValueError(f"{column} must be one of {allowed} (got {value!r})") from None
    for column in _BOOL_COLUMNS:
        if value := row.get(column, "").strip():
            changes[column] = _parse_bool(value, column)
    request = msgspec.structs.replace(defaults, **changes)
    request.validate()
    return request


def _read_rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    text = path.read_text(encoding="utf-8-sig")
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    if path.suffix.lower() == ".csv" or "prompt" in first_line.lower().split(","):
        reader = csv.DictReader(text.splitlines())
        fields = {name.strip().lower() for name in reader.fieldnames or []}
        if "prompt" not in fields:
            raise TopicsFileError(path, ["the CSV needs a 'prompt' column"])
        unknown = sorted(fields - KNOWN_COLUMNS)
        if unknown:
            raise TopicsFileError(path, [f"unknown columns: {', '.join(unknown)}"])
        return [
            (line, {(k or "").strip().lower(): (v or "") for k, v in row.items()})
            for line, row in enumerate(reader, start=2)
            if (row.get("prompt") or "").strip()
        ]
    return [
        (line, {"prompt": raw.strip()})
        for line, raw in enumerate(text.splitlines(), start=1)
        if raw.strip() and not raw.lstrip().startswith("#")
    ]


def read_topics(path: Path, defaults: VideoRequest) -> list[Topic]:
    """Read every topic. Raise TopicsFileError that lists all problems with line numbers."""
    topics: list[Topic] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for line, row in _read_rows(path):
        try:
            request = _row_request(row, defaults)
        except ValueError as error:
            problems.append(f"line {line}: {error}")
            continue
        name = folder_name_for(request.prompt, row.get("id", "").strip() or None)
        if name in seen:
            problems.append(f"line {line}: same topic as line {seen[name]}")
            continue
        seen[name] = line
        topics.append(Topic(folder_name=name, request=request))
    if problems:
        raise TopicsFileError(path, problems)
    return topics
