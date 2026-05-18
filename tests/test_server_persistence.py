"""Tests for comfy_runner_server.persistence and the wiring in server.py.

Covers:
- Roundtrip load/save of the test-runs registry.
- Schema-version mismatch is treated as missing (fail-open to empty).
- Corrupt JSON does not crash load.
- The DebouncedSaver coalesces bursty schedule() calls.
- create_app() loads persisted runs into _test_runs.
- _register_test_run / _finish_test_run trigger a persisted snapshot.
- _gc_test_runs evicts entries older than the age cap AND beyond size cap.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from comfy_runner_server import persistence
from comfy_runner_server import server as srv


@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """Point persistence at a temp dir for the duration of the test.

    Stops any pending background timer on the previously-cached saver
    before zeroing the module slot — otherwise the orphaned Timer would
    fire up to ``DebouncedSaver._delay`` seconds later against a
    torn-down temp dir and could flake adjacent tests.
    """
    monkeypatch.setenv("COMFY_RUNNER_HOME", str(tmp_path))
    # Cancel any pending write from a prior test before swapping the
    # saver out. ``flush`` cancels the timer and synchronously writes
    # whatever's cached; the snapshot is empty by then because clean_test_runs
    # ran first, so this is a cheap no-op.
    prev_saver = getattr(srv, "_saver_instance", None)
    if prev_saver is not None:
        try:
            prev_saver.flush()
        except Exception:
            pass
    monkeypatch.setattr(srv, "_saver_instance", None)
    monkeypatch.delenv("COMFY_RUNNER_DISABLE_TEST_RUN_PERSISTENCE", raising=False)
    yield tmp_path
    # Tear-down: cancel any timer the test scheduled so it cannot fire
    # against ``tmp_path`` after pytest has cleaned it up.
    fresh_saver = getattr(srv, "_saver_instance", None)
    if fresh_saver is not None and fresh_saver is not prev_saver:
        try:
            fresh_saver.flush()
        except Exception:
            pass


@pytest.fixture
def clean_test_runs():
    """Wrap each test so the global _test_runs dict is isolated."""
    with srv._test_runs_lock:
        snapshot = dict(srv._test_runs)
        srv._test_runs.clear()
    try:
        yield
    finally:
        with srv._test_runs_lock:
            srv._test_runs.clear()
            srv._test_runs.update(snapshot)


# ---------------------------------------------------------------------------
# Pure persistence module
# ---------------------------------------------------------------------------

class TestLoadSave:
    def test_load_missing_file_returns_empty(self, isolated_state_dir):
        assert persistence.load_test_runs() == {}

    def test_save_then_load_roundtrip(self, isolated_state_dir):
        runs = {
            "abc": {"id": "abc", "status": "done", "created_at": 1.0},
            "xyz": {"id": "xyz", "status": "running", "created_at": 2.0},
        }
        persistence.save_test_runs(runs)
        loaded = persistence.load_test_runs()
        assert loaded == runs

    def test_load_wrong_schema_version_returns_empty(self, isolated_state_dir):
        path = persistence.test_runs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 999, "runs": {"a": {"id": "a"}}}))
        assert persistence.load_test_runs() == {}

    def test_load_corrupt_json_returns_empty(self, isolated_state_dir):
        path = persistence.test_runs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")
        assert persistence.load_test_runs() == {}

    def test_load_non_dict_runs_returns_empty(self, isolated_state_dir):
        path = persistence.test_runs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "runs": "oops"}))
        assert persistence.load_test_runs() == {}

    def test_load_drops_non_dict_entries(self, isolated_state_dir):
        path = persistence.test_runs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1,
            "runs": {"good": {"id": "good"}, "bad": "not-a-dict"},
        }))
        assert persistence.load_test_runs() == {"good": {"id": "good"}}

    def test_save_non_json_serializable_uses_default_str(self, isolated_state_dir):
        runs = {"a": {"id": "a", "path": Path("/some/path")}}
        persistence.save_test_runs(runs)
        loaded = persistence.load_test_runs()
        # Path coerced to str via json default=str.
        assert loaded["a"]["path"] == str(Path("/some/path"))


# ---------------------------------------------------------------------------
# DebouncedSaver
# ---------------------------------------------------------------------------

class TestDebouncedSaver:
    def test_coalesces_multiple_schedule_calls(self):
        writes: list[dict[str, Any]] = []
        def fake_save(data: Any) -> None:
            writes.append(data)
        saver = persistence.DebouncedSaver(
            snapshot=lambda: {"a": 1}, delay=0.05, save_fn=fake_save,
        )
        for _ in range(20):
            saver.schedule()
        time.sleep(0.2)
        # Only one trailing write should have fired despite 20 schedule calls.
        assert len(writes) == 1

    def test_flush_runs_synchronously(self):
        writes: list[dict[str, Any]] = []
        saver = persistence.DebouncedSaver(
            snapshot=lambda: {"k": "v"}, delay=10.0,
            save_fn=lambda d: writes.append(d),
        )
        saver.schedule()
        saver.flush()
        assert writes == [{"k": "v"}]

    def test_snapshot_failure_is_swallowed(self):
        def broken_snapshot() -> dict[str, Any]:
            raise RuntimeError("snap boom")
        saver = persistence.DebouncedSaver(
            snapshot=broken_snapshot, delay=0.05,
            save_fn=lambda d: None,
        )
        saver.schedule()
        time.sleep(0.15)  # snapshot raises; saver must not crash the thread.


# ---------------------------------------------------------------------------
# Server wiring
# ---------------------------------------------------------------------------

class TestServerWiring:
    def test_register_test_run_schedules_save(
        self, isolated_state_dir, clean_test_runs,
    ):
        srv._register_test_run("t-1", {"kind": "single", "status": "running"})
        # Force the debounced write to happen now rather than wait 2s.
        srv._persistence_saver().flush()
        loaded = persistence.load_test_runs()
        assert "t-1" in loaded
        assert loaded["t-1"]["kind"] == "single"

    def test_finish_test_run_updates_persisted_record(
        self, isolated_state_dir, clean_test_runs,
    ):
        srv._register_test_run("t-2", {"kind": "fleet"})
        srv._finish_test_run("t-2", {"summary": {"total_targets": 5}}, status="done")
        srv._persistence_saver().flush()
        loaded = persistence.load_test_runs()
        assert loaded["t-2"]["status"] == "done"
        assert loaded["t-2"]["summary"]["total_targets"] == 5

    def test_create_app_loads_persisted_runs(
        self, isolated_state_dir, clean_test_runs,
    ):
        # Pre-seed the persistence file with a record from a "previous boot".
        # Use a fresh timestamp so the age-cap GC doesn't immediately drop it.
        persistence.save_test_runs({
            "old-run": {"id": "old-run", "status": "done", "created_at": time.time()},
        })
        # Simulate restart: create a new Flask app instance.
        srv.create_app()
        with srv._test_runs_lock:
            assert "old-run" in srv._test_runs
            assert srv._test_runs["old-run"]["status"] == "done"

    def test_disable_env_skips_persistence(
        self, isolated_state_dir, clean_test_runs, monkeypatch,
    ):
        # When disabled, _schedule_test_runs_save short-circuits and the
        # saver is never built — confirmed by leaving the file untouched
        # after a register call.
        monkeypatch.setenv("COMFY_RUNNER_DISABLE_TEST_RUN_PERSISTENCE", "1")
        srv._register_test_run("t-3", {"kind": "single"})
        # The saver instance must not have been constructed.
        assert srv._saver_instance is None
        # And the on-disk file must not exist.
        assert not persistence.test_runs_path().exists()


# ---------------------------------------------------------------------------
# _gc_test_runs — age + size limits
# ---------------------------------------------------------------------------

class TestGCTestRuns:
    def test_evicts_runs_older_than_age_cap(
        self, isolated_state_dir, clean_test_runs, monkeypatch,
    ):
        # Speed up by shrinking the age cap.
        monkeypatch.setattr(srv, "_MAX_TEST_RUN_AGE_S", 1.0)
        with srv._test_runs_lock:
            srv._test_runs["fresh"] = {"id": "fresh", "created_at": time.time()}
            srv._test_runs["old"] = {"id": "old", "created_at": time.time() - 100}
            srv._gc_test_runs()
            keys = set(srv._test_runs.keys())
        assert "fresh" in keys
        assert "old" not in keys

    def test_evicts_oldest_when_over_size_cap(
        self, isolated_state_dir, clean_test_runs, monkeypatch,
    ):
        monkeypatch.setattr(srv, "_MAX_TEST_RUNS", 3)
        # Disable the age cap with a value larger than any plausible
        # ``now - created_at`` (>30 000 years) so we exercise the
        # size-cap branch in isolation.
        monkeypatch.setattr(srv, "_MAX_TEST_RUN_AGE_S", 10**12)
        base = time.time()
        with srv._test_runs_lock:
            for i in range(5):
                srv._test_runs[f"r{i}"] = {"id": f"r{i}", "created_at": base + i}
            srv._gc_test_runs()
            keys = set(srv._test_runs.keys())
        # r0 and r1 are oldest → evicted; r2, r3, r4 remain.
        assert keys == {"r2", "r3", "r4"}

    def test_no_op_when_under_caps(
        self, isolated_state_dir, clean_test_runs,
    ):
        with srv._test_runs_lock:
            srv._test_runs["only"] = {"id": "only", "created_at": time.time()}
            srv._gc_test_runs()
        assert "only" in srv._test_runs
