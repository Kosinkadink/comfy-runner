"""Tests for comfy_runner.log_utils module."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from comfy_runner.log_utils import (
    LOG_FILENAME,
    _ROTATED_RE,
    list_log_sessions,
    open_log,
    read_current_log,
    read_log_after,
    rotate_log,
)


# ---------------------------------------------------------------------------
# _ROTATED_RE
# ---------------------------------------------------------------------------

class TestRotatedRe:
    @pytest.mark.parametrize("name", [
        ".comfy-runner_2026-03-30T14-23-15.log",
        ".comfy-runner_2000-01-01T00-00-00.log",
    ])
    def test_matches_valid(self, name: str) -> None:
        assert _ROTATED_RE.match(name)

    @pytest.mark.parametrize("name", [
        ".comfy-runner.log",
        "comfy-runner_2026-03-30T14-23-15.log",
        ".comfy-runner_2026-03-30.log",
        ".comfy-runner_bad.log",
    ])
    def test_rejects_invalid(self, name: str) -> None:
        assert not _ROTATED_RE.match(name)


# ---------------------------------------------------------------------------
# rotate_log
# ---------------------------------------------------------------------------

class TestRotateLog:
    def test_renames_to_timestamped(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        log.write_text("session data\n")

        rotate_log(tmp_path)

        assert not log.exists()
        rotated = [f for f in tmp_path.iterdir() if _ROTATED_RE.match(f.name)]
        assert len(rotated) == 1
        assert rotated[0].read_text() == "session data\n"

    def test_skips_missing_log(self, tmp_path: Path) -> None:
        rotate_log(tmp_path)
        # No error, nothing created
        assert list(tmp_path.iterdir()) == []

    def test_skips_empty_log(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        log.write_text("")

        rotate_log(tmp_path)
        # Original still there, no rotated file
        rotated = [f for f in tmp_path.iterdir() if _ROTATED_RE.match(f.name)]
        assert rotated == []

    def test_prunes_old_rotated(self, tmp_path: Path) -> None:
        # Create 5 existing rotated logs
        for i in range(5):
            (tmp_path / f".comfy-runner_2026-01-{i+1:02d}T00-00-00.log").write_text(f"old {i}")

        # Write current log and rotate with max_files=3
        log = tmp_path / LOG_FILENAME
        log.write_text("current session")

        rotate_log(tmp_path, max_files=3)

        rotated = [f for f in tmp_path.iterdir() if _ROTATED_RE.match(f.name)]
        assert len(rotated) <= 3


# ---------------------------------------------------------------------------
# open_log
# ---------------------------------------------------------------------------

class TestOpenLog:
    def test_rotates_and_opens_fresh(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        log.write_text("old data\n")

        fh, path = open_log(tmp_path)
        try:
            assert path == log
            fh.write("new data\n")
        finally:
            fh.close()

        assert log.read_text() == "new data\n"
        # Old data should be in a rotated file
        rotated = [f for f in tmp_path.iterdir() if _ROTATED_RE.match(f.name)]
        assert len(rotated) == 1
        assert rotated[0].read_text() == "old data\n"


# ---------------------------------------------------------------------------
# read_current_log
# ---------------------------------------------------------------------------

class TestReadCurrentLog:
    def test_returns_lines_and_size(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        log.write_text("line1\nline2\nline3\n")

        result = read_current_log(tmp_path)
        assert result["lines"] == ["line1", "line2", "line3"]
        assert result["size"] == log.stat().st_size
        assert result["path"] == str(log)

    def test_empty_when_no_log(self, tmp_path: Path) -> None:
        result = read_current_log(tmp_path)
        assert result["lines"] == []
        assert result["size"] == 0

    def test_respects_max_lines(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        log.write_text("\n".join(f"line{i}" for i in range(20)) + "\n")

        result = read_current_log(tmp_path, max_lines=5)
        assert len(result["lines"]) == 5
        assert result["lines"][-1] == "line19"


# ---------------------------------------------------------------------------
# read_log_after
# ---------------------------------------------------------------------------

class TestReadLogAfter:
    def test_returns_new_content(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        initial = "line1\nline2\n"
        log.write_text(initial)
        offset = len(initial.encode("utf-8"))

        # Append more content
        with open(log, "a") as f:
            f.write("line3\nline4\n")

        result = read_log_after(tmp_path, offset)
        assert result["lines"] == ["line3", "line4"]
        assert result["offset"] > offset

    def test_returns_empty_at_end(self, tmp_path: Path) -> None:
        log = tmp_path / LOG_FILENAME
        log.write_text("hello\n")
        size = log.stat().st_size

        result = read_log_after(tmp_path, size)
        assert result["lines"] == []
        assert result["offset"] == size



# ---------------------------------------------------------------------------
# list_log_sessions
# ---------------------------------------------------------------------------

class TestListLogSessions:
    def test_lists_current_and_rotated(self, tmp_path: Path) -> None:
        current = tmp_path / LOG_FILENAME
        current.write_text("current session\n")

        rot1 = tmp_path / ".comfy-runner_2026-01-01T00-00-00.log"
        rot1.write_text("session 1\n")
        rot2 = tmp_path / ".comfy-runner_2026-01-02T00-00-00.log"
        rot2.write_text("session 2\n")

        sessions = list_log_sessions(tmp_path)
        assert len(sessions) == 3

        # First should be current
        assert sessions[0]["current"] is True
        assert sessions[0]["filename"] == LOG_FILENAME

        # Rotated should be newest-first
        assert sessions[1]["current"] is False
        assert sessions[1]["filename"] == rot2.name
        assert sessions[2]["current"] is False
        assert sessions[2]["filename"] == rot1.name

    def test_empty(self, tmp_path: Path) -> None:
        assert list_log_sessions(tmp_path) == []
