from __future__ import annotations

from pathlib import Path

import pytest

from whiteboard_video.models import Orientation, Style, VideoRequest, Voice
from whiteboard_video.topics import TopicsFileError, read_topics

DEFAULTS = VideoRequest(prompt="placeholder", seconds=60, voice=Voice.FEMALE)


def test_text_file_uses_one_topic_per_line_and_skips_comments(tmp_path: Path) -> None:
    path = tmp_path / "topics.txt"
    path.write_text("# my channel\nHow black holes form\n\nWhy the sky is blue\n")

    topics = read_topics(path, DEFAULTS)

    assert [t.request.prompt for t in topics] == ["How black holes form", "Why the sky is blue"]
    assert all(t.request.voice is Voice.FEMALE for t in topics)
    assert topics[0].folder_name.startswith("how-black-holes-form-")


def test_csv_columns_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "topics.csv"
    path.write_text(
        "id,prompt,seconds,orientation,style,music\n"
        "ep-01,How black holes form,30,portrait,animate,no\n"
        ",Why the sky is blue,,,,\n"
    )

    first, second = read_topics(path, DEFAULTS)

    assert first.folder_name == "ep-01"
    assert first.request.seconds == 30
    assert first.request.orientation is Orientation.PORTRAIT
    assert first.request.style is Style.ANIMATE
    assert first.request.music is False
    assert second.request.seconds == 60
    assert second.request.orientation is Orientation.LANDSCAPE


def test_reports_every_problem_with_its_line(tmp_path: Path) -> None:
    path = tmp_path / "topics.csv"
    path.write_text(
        "prompt,seconds,style\n"
        "How black holes form,45,whiteboard\n"
        "Why the sky is blue,60,oil-painting\n"
        "ok,60,whiteboard\n"
        "How black holes form,45,whiteboard\n"
    )

    with pytest.raises(TopicsFileError) as caught:
        read_topics(path, DEFAULTS)

    problems = caught.value.problems
    assert problems[0].startswith("line 2: Video length must be one of")
    assert problems[1].startswith("line 3: style must be one of")
    assert problems[2].startswith("line 4: The prompt must have 3")
    assert len(problems) == 4


def test_unknown_columns_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "topics.csv"
    path.write_text("prompt,lenght\nHow black holes form,60\n")

    with pytest.raises(TopicsFileError, match="lenght"):
        read_topics(path, DEFAULTS)


def test_duplicate_topics_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "topics.txt"
    path.write_text("How black holes form\nHow black holes form\n")

    with pytest.raises(TopicsFileError, match="same topic as line 1"):
        read_topics(path, DEFAULTS)
