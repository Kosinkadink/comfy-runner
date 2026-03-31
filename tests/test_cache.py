from __future__ import annotations

import json
import time

import pytest

import comfy_runner.cache as cache_mod
from comfy_runner.cache import (
    DEFAULT_MAX_ENTRIES,
    _dir_size,
    evict,
    get_cache_path,
    touch,
)


class TestGetCachePath:
    def test_creates_directory(self, tmp_config_dir):
        p = get_cache_path("my-key")
        assert p.exists()
        assert p.is_dir()
        assert p.name == "my-key"


class TestTouch:
    def test_updates_metadata(self, tmp_config_dir):
        get_cache_path("entry1")
        before = time.time()
        touch("entry1")
        after = time.time()

        meta = json.loads(cache_mod.CACHE_META_FILE.read_text(encoding="utf-8"))
        assert "entry1" in meta
        assert before <= meta["entry1"]["last_used"] <= after


class TestEvict:
    def test_removes_oldest_over_budget(self, tmp_config_dir):
        # Create two cache entries with known sizes
        p1 = get_cache_path("old")
        (p1 / "data.bin").write_bytes(b"x" * 500)
        touch("old")

        # Small sleep so timestamps differ
        time.sleep(0.05)

        p2 = get_cache_path("new")
        (p2 / "data.bin").write_bytes(b"y" * 500)
        touch("new")

        # Budget = 600 bytes — should evict "old" (total 1000 > 600)
        evict(max_bytes=600)

        assert not p1.exists(), "oldest entry should be evicted"
        assert p2.exists(), "newest entry should survive"

    def test_no_eviction_under_budget(self, tmp_config_dir):
        p = get_cache_path("keep")
        (p / "data.bin").write_bytes(b"z" * 100)
        touch("keep")

        evict(max_bytes=10000)
        assert p.exists()

    def test_evicts_oldest_over_max_entries(self, tmp_config_dir):
        paths = []
        for i, name in enumerate(["a", "b", "c", "d"]):
            p = get_cache_path(name)
            (p / "data.bin").write_bytes(b"x" * 10)
            touch(name)
            paths.append(p)
            time.sleep(0.05)

        # max_entries=2 should evict the two oldest ("a" and "b")
        evict(max_entries=2, max_bytes=10 * 1024 * 1024 * 1024)

        assert not paths[0].exists(), "'a' should be evicted"
        assert not paths[1].exists(), "'b' should be evicted"
        assert paths[2].exists(), "'c' should survive"
        assert paths[3].exists(), "'d' should survive"

    def test_evicts_by_both_entries_and_bytes(self, tmp_config_dir):
        """When both limits are exceeded, keep evicting until both are met."""
        p1 = get_cache_path("old")
        (p1 / "data.bin").write_bytes(b"x" * 600)
        touch("old")
        time.sleep(0.05)

        p2 = get_cache_path("mid")
        (p2 / "data.bin").write_bytes(b"y" * 600)
        touch("mid")
        time.sleep(0.05)

        p3 = get_cache_path("new")
        (p3 / "data.bin").write_bytes(b"z" * 600)
        touch("new")

        # max_entries=3 is fine, but max_bytes=700 forces eviction
        evict(max_entries=3, max_bytes=700)

        assert not p1.exists()
        assert not p2.exists()
        assert p3.exists()

    def test_no_eviction_under_both_limits(self, tmp_config_dir):
        for name in ["x", "y"]:
            p = get_cache_path(name)
            (p / "data.bin").write_bytes(b"a" * 50)
            touch(name)

        evict(max_entries=5, max_bytes=10000)
        assert (cache_mod.CACHE_DIR / "x").exists()
        assert (cache_mod.CACHE_DIR / "y").exists()

    def test_default_max_entries_is_three(self):
        assert DEFAULT_MAX_ENTRIES == 3


class TestDirSize:
    def test_calculates_correctly(self, tmp_path):
        d = tmp_path / "sized"
        d.mkdir()
        (d / "a.txt").write_bytes(b"a" * 100)
        (d / "b.txt").write_bytes(b"b" * 200)
        sub = d / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c" * 50)

        assert _dir_size(d) == 350
