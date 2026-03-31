"""Tests for comfy_runner_server.server — unit + Flask integration."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Ensure comfy-runner root is on sys.path (for safe_file, comfy_runner, etc.)
_RUNNER_ROOT = Path(__file__).resolve().parent.parent
if str(_RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNNER_ROOT))

from comfy_runner_server.server import _make_collector, _JobTracker


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture()
def app(tmp_config_dir):
    """Flask app with config pointed at a temp directory."""
    from comfy_runner_server.server import create_app

    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


# =====================================================================
# _make_collector
# =====================================================================


class TestMakeCollector:
    def test_collects_text(self):
        send, lines = _make_collector()
        send("hello")
        send("world")
        assert lines == ["hello", "world"]

    def test_thread_safe(self):
        import threading

        send, lines = _make_collector()
        barrier = threading.Barrier(4)

        def append_many(prefix: str) -> None:
            barrier.wait()
            for i in range(50):
                send(f"{prefix}-{i}")

        threads = [threading.Thread(target=append_many, args=(f"t{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(lines) == 200


# =====================================================================
# _JobTracker
# =====================================================================


class TestJobTracker:
    def test_create_returns_running_job(self):
        tracker = _JobTracker()
        job_id = tracker.create(label="test-job")
        job = tracker.get(job_id)
        assert job is not None
        assert job["status"] == "running"
        assert job["label"] == "test-job"
        assert job["result"] is None
        assert job["error"] is None

    def test_finish_sets_done(self):
        tracker = _JobTracker()
        job_id = tracker.create()
        tracker.finish(job_id, {"key": "val"}, ["line1"])
        job = tracker.get(job_id)
        assert job["status"] == "done"
        assert job["result"] == {"key": "val"}
        assert job["output"] == ["line1"]
        assert job["finished_at"] is not None

    def test_fail_sets_error(self):
        tracker = _JobTracker()
        job_id = tracker.create()
        tracker.fail(job_id, "something broke", ["out1"])
        job = tracker.get(job_id)
        assert job["status"] == "error"
        assert job["error"] == "something broke"
        assert job["output"] == ["out1"]

    def test_cancel_sets_cancelled(self):
        tracker = _JobTracker()
        job_id = tracker.create()
        result = tracker.cancel(job_id)
        assert result is True
        job = tracker.get(job_id)
        assert job["status"] == "cancelled"
        assert job["finished_at"] is not None

    def test_cancel_already_done_returns_false(self):
        tracker = _JobTracker()
        job_id = tracker.create()
        tracker.finish(job_id, {}, [])
        assert tracker.cancel(job_id) is False

    def test_get_returns_none_for_missing(self):
        tracker = _JobTracker()
        assert tracker.get("no-such-id") is None

    def test_list_active_returns_all_jobs(self):
        tracker = _JobTracker()
        id1 = tracker.create(label="a")
        id2 = tracker.create(label="b")
        tracker.finish(id2, {}, [])
        active = tracker.list_active()
        assert len(active) == 2
        labels = {j["label"] for j in active}
        assert labels == {"a", "b"}
        # list_active strips output
        for j in active:
            assert "output" not in j

    def test_gc_removes_expired_jobs(self):
        tracker = _JobTracker(ttl=0)
        job_id = tracker.create()
        tracker.finish(job_id, {}, [])
        time.sleep(0.01)
        assert tracker.get(job_id) is None

    def test_gc_keeps_running_jobs(self):
        tracker = _JobTracker(ttl=0)
        job_id = tracker.create()
        time.sleep(0.01)
        assert tracker.get(job_id) is not None

    def test_get_cancel_event(self):
        tracker = _JobTracker()
        job_id = tracker.create()
        evt = tracker.get_cancel_event(job_id)
        assert evt is not None
        assert not evt.is_set()
        tracker.cancel(job_id)
        assert evt.is_set()


# =====================================================================
# Flask integration tests
# =====================================================================


class TestFlaskApp:
    def test_get_installations_empty(self, client):
        resp = client.get("/installations")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["installations"] == []

    def test_get_installations_with_entries(self, client, tmp_config_dir, monkeypatch):
        import comfy_runner.config as cfg_mod

        config_file = cfg_mod.CONFIG_FILE
        config_file.write_text(json.dumps({
            "installations_dir": str(cfg_mod.CONFIG_DIR / "installations"),
            "installations": {
                "test-inst": {
                    "path": "/tmp/fake-install",
                    "variant": "linux-x86_64-cu126",
                    "status": "installed",
                },
            },
            "tunnel": {},
            "shared_dir": "",
        }))

        import comfy_runner.process as proc_mod
        import comfy_runner.tunnel as tunnel_mod

        monkeypatch.setattr(proc_mod, "get_status", lambda name: {"running": False})
        monkeypatch.setattr(tunnel_mod, "get_tunnel_url", lambda name: None)

        resp = client.get("/installations")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert len(data["installations"]) == 1
        assert data["installations"][0]["name"] == "test-inst"

    def test_get_status_404_for_nonexistent(self, client):
        resp = client.get("/nonexistent/status")
        data = resp.get_json()
        assert resp.status_code == 404
        assert data["ok"] is False
        assert "not found" in data["error"].lower()

    def test_get_health_returns_json_error(self, client):
        # No /health route — "health" matches /<name> wildcard so Flask
        # resolves to a POST-only route, yielding 405 (not 404). Either way
        # the response must be JSON, not HTML.
        resp = client.get("/health")
        data = resp.get_json()
        assert resp.status_code in (404, 405)
        assert data["ok"] is False
        assert resp.content_type.startswith("application/json")

    def test_get_job_404_for_nonexistent(self, client):
        resp = client.get("/job/does-not-exist")
        data = resp.get_json()
        assert resp.status_code == 404
        assert data["ok"] is False
        assert "not found" in data["error"].lower()

    def test_post_stop_error_for_nonexistent(self, client):
        resp = client.post("/nonexistent/stop")
        data = resp.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"].lower()

    def test_get_system_info(self, client, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.system_info.detect_gpu", lambda: "cpu"
        )
        monkeypatch.setattr(
            "comfy_runner.system_info._get_gpus", lambda: []
        )
        resp = client.get("/system-info")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert "system_info" in data
        si = data["system_info"]
        assert "gpu_vendor" in si
        assert "cpu_model" in si
        assert "total_memory_gb" in si
        assert "disk_free_gb" in si

    def test_unknown_route_returns_json_not_html(self, client):
        resp = client.get("/this/route/does/not/exist")
        assert resp.status_code == 404
        assert resp.content_type.startswith("application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "error" in data
