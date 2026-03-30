from __future__ import annotations

import sys
from pathlib import Path

import pytest

COMFY_RUNNER_ROOT = Path(__file__).resolve().parent.parent

# Ensure safe_file is importable
if str(COMFY_RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(COMFY_RUNNER_ROOT))

from safe_file import atomic_read, atomic_write


class TestAtomicWrite:
    def test_writes_content(self, tmp_path):
        p = tmp_path / "hello.txt"
        atomic_write(p, "hello world")
        assert p.read_text(encoding="utf-8") == "hello world"

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "c" / "file.txt"
        atomic_write(p, "nested")
        assert p.read_text(encoding="utf-8") == "nested"

    def test_backup_creates_bak(self, tmp_path):
        p = tmp_path / "data.json"
        atomic_write(p, "original")
        atomic_write(p, "updated", backup=True)

        bak = Path(str(p) + ".bak")
        assert p.read_text(encoding="utf-8") == "updated"
        assert bak.read_text(encoding="utf-8") == "original"


class TestAtomicRead:
    def test_reads_normal_file(self, tmp_path):
        p = tmp_path / "read.txt"
        p.write_text("content", encoding="utf-8")
        assert atomic_read(p) == "content"

    def test_falls_back_to_bak(self, tmp_path):
        p = tmp_path / "missing.txt"
        bak = Path(str(p) + ".bak")
        bak.write_text("backup-data", encoding="utf-8")

        result = atomic_read(p)
        assert result == "backup-data"
        # Should also restore primary
        assert p.read_text(encoding="utf-8") == "backup-data"

    def test_returns_none_when_both_missing(self, tmp_path):
        p = tmp_path / "nope.txt"
        assert atomic_read(p) is None
