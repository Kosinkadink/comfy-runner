"""Tests for comfy_runner_server.server — unit + Flask integration."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

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


# =====================================================================
# POST /reviews/local — sidecar review prep endpoint (item 2)
# =====================================================================


def _wait_job(client, job_id: str, timeout: float = 5.0) -> dict:
    """Poll /job/<job_id> until status != 'running' or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/job/{job_id}")
        data = resp.get_json()
        assert data["ok"] is True, data
        if data.get("status") != "running":
            return data
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} still running after {timeout}s")


class TestReviewsLocal:
    def _body(self, **overrides) -> dict:
        body = {
            "install": "main",
            "owner": "comfy-org",
            "repo": "ComfyUI",
            "pr": 42,
        }
        body.update(overrides)
        return body

    def test_happy_path_runs_prepare_and_finishes_job(
        self, client, fake_install, monkeypatch,
    ):
        review_result = {
            "manifest": {"models": [], "workflows": []},
            "resolved": None,
            "downloaded": ["checkpoints/x"],
            "skipped": [],
            "failed": [],
            "errors": [],
            "workflows": [],
            "workflows_dir": str(fake_install),
            "failures": [],
        }
        monkeypatch.setattr(
            "comfy_runner.review.prepare_local_review",
            lambda *a, **kw: review_result,
        )

        resp = client.post("/reviews/local", json=self._body())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["async"] is True
        job_id = data["job_id"]

        final = _wait_job(client, job_id)
        assert final["status"] == "done"
        assert final["result"] == review_result

    def test_install_404_for_unknown(self, client, tmp_config_dir):
        resp = client.post("/reviews/local", json=self._body(install="nope"))
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_install_rejects_path_traversal(self, client, tmp_config_dir):
        resp = client.post(
            "/reviews/local", json=self._body(install=".."),
        )
        assert resp.status_code == 400
        assert "safe identifier" in resp.get_json()["error"]

    def test_missing_pr_rejected(self, client, fake_install):
        body = self._body()
        del body["pr"]
        resp = client.post("/reviews/local", json=body)
        assert resp.status_code == 400
        assert "pr" in resp.get_json()["error"].lower()

    def test_negative_pr_rejected(self, client, fake_install):
        resp = client.post("/reviews/local", json=self._body(pr=-1))
        assert resp.status_code == 400

    def test_pr_must_be_int_not_bool(self, client, fake_install):
        # Without this guard, JSON booleans would be accepted as ints.
        resp = client.post("/reviews/local", json=self._body(pr=True))
        assert resp.status_code == 400

    def test_missing_owner_rejected(self, client, fake_install):
        body = self._body()
        del body["owner"]
        resp = client.post("/reviews/local", json=body)
        assert resp.status_code == 400

    def test_extra_models_must_be_list(self, client, fake_install):
        resp = client.post(
            "/reviews/local", json=self._body(extra_models="not-a-list"),
        )
        assert resp.status_code == 400

    def test_extra_workflows_must_be_list_of_strings(self, client, fake_install):
        resp = client.post(
            "/reviews/local", json=self._body(extra_workflows=[1, 2, 3]),
        )
        assert resp.status_code == 400

    def test_invalid_extra_models_rejected(self, client, fake_install):
        resp = client.post(
            "/reviews/local",
            json=self._body(extra_models=[{"missing": "fields"}]),
        )
        assert resp.status_code == 400
        assert "extra_models" in resp.get_json()["error"]

    def test_extras_threaded_to_prepare(
        self, client, fake_install, monkeypatch,
    ):
        captured: dict = {}

        def fake_prepare(install_path, owner, repo, pr, **kw):
            captured["args"] = (install_path, owner, repo, pr)
            captured["kwargs"] = kw
            return {
                "manifest": None, "resolved": None,
                "downloaded": [], "skipped": [], "failed": [], "errors": [],
                "workflows": [], "workflows_dir": "/x", "failures": [],
            }
        monkeypatch.setattr(
            "comfy_runner.review.prepare_local_review", fake_prepare,
        )

        resp = client.post("/reviews/local", json=self._body(
            extra_workflows=["https://h/wf.json"],
            extra_models=[{"name": "m.safetensors", "url": "https://h/m",
                           "directory": "loras"}],
            github_token="ghp",
            download_token="hf",
            allow_arbitrary_urls=True,
            skip_provisioning=True,
        ))
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id)
        assert final["status"] == "done"

        assert captured["args"][1] == "comfy-org"
        assert captured["args"][2] == "ComfyUI"
        assert captured["args"][3] == 42
        kw = captured["kwargs"]
        assert kw["github_token"] == "ghp"
        assert kw["download_token"] == "hf"
        assert kw["allow_arbitrary_urls"] is True
        assert kw["skip_provisioning"] is True
        assert kw["extra_workflows"] == ["https://h/wf.json"]
        assert len(kw["extra_models"]) == 1
        assert kw["extra_models"][0].name == "m.safetensors"

    def test_prepare_exception_fails_job(
        self, client, fake_install, monkeypatch,
    ):
        def boom(*a, **kw):
            raise RuntimeError("disk full")
        monkeypatch.setattr(
            "comfy_runner.review.prepare_local_review", boom,
        )

        resp = client.post("/reviews/local", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id)
        assert final["status"] == "error"
        assert "disk full" in final["error"]


# =====================================================================
# POST /pods/<name>/review — station-mediated review prep (item 2)
# =====================================================================


class _FakePod:
    def __init__(self, status: str = "RUNNING") -> None:
        self.status = status


class TestPodsReview:
    """``POST /pods/<name>/review`` — auto-wake + deploy + review proxy."""

    def _setup_pod(
        self, monkeypatch, status: str = "RUNNING",
        purpose: str | None = None,
    ):
        """Register a pod record + stub the runpod provider."""
        from comfy_runner.hosted import config as hcfg
        # Create a pod record via the existing hosted config API.
        rec: dict = {
            "id": "pod-id-123", "name": "pod-a", "gpu_type": "RTX_4090",
        }
        if purpose is not None:
            rec["purpose"] = purpose
        monkeypatch.setattr(
            hcfg, "get_pod_record",
            lambda provider, name: rec if name == "pod-a" else None,
        )

        provider = MagicMock()
        provider.get_pod = MagicMock(return_value=_FakePod(status))
        provider.start_pod = MagicMock()
        # Patch the lazy provider getter inside server.py.
        monkeypatch.setattr(
            "comfy_runner_server.server._get_runpod_provider",
            lambda: provider,
        )
        # Resolve the pod's sidecar URL deterministically.
        monkeypatch.setattr(
            "comfy_runner_server.server._get_pod_server_url",
            lambda name, **_: "https://pod-a.ts.net:9189",
        )
        monkeypatch.setattr(
            "comfy_runner_server.server._wait_for_remote_server",
            lambda url, **_: None,
        )
        monkeypatch.setattr(
            "comfy_runner_server.server._touch_pod_activity",
            lambda name: None,
        )
        return provider

    def _setup_runner(self, monkeypatch, *,
                       deploy_result=None, review_result=None,
                       deploy_job=True, review_job=True,
                       deployed_pr_on_pod=None,
                       deployed_repo_on_pod="",
                       ):
        runner = MagicMock()

        # Idempotency-check call: GET /<install>/info. By default we
        # return an info dict with no ``deployed_pr`` so the check
        # returns False and the worker proceeds to deploy. Tests can
        # override deployed_pr_on_pod to simulate a pod that already
        # has the PR deployed.
        info_resp: dict = {"ok": True, "name": "main"}
        if deployed_pr_on_pod is not None:
            info_resp["deployed_pr"] = deployed_pr_on_pod
            info_resp["deployed_repo"] = deployed_repo_on_pod

        # First call → info (idempotency check)
        # Second call → deploy job
        # Third call → review job
        responses: list = [info_resp]
        if deploy_job:
            responses.append({"ok": True, "job_id": "deploy-1"})
        else:
            responses.append({"ok": True})
        if review_job:
            responses.append({"ok": True, "job_id": "review-1"})
        else:
            responses.append({"ok": True})
        runner._request = MagicMock(side_effect=responses)

        results = [
            deploy_result or {"restarted": True},
            review_result or {
                "manifest": None, "resolved": None,
                "downloaded": [], "skipped": [], "failed": [], "errors": [],
                "workflows": [], "workflows_dir": "/x", "failures": [],
            },
        ]
        runner.poll_job = MagicMock(side_effect=results)
        monkeypatch.setattr(
            "comfy_runner.hosted.remote.RemoteRunner",
            lambda url: runner,
        )
        return runner

    def _body(self, **overrides) -> dict:
        body = {"owner": "comfy-org", "repo": "ComfyUI", "pr": 99}
        body.update(overrides)
        return body

    def test_happy_path_running_pod(self, client, tmp_config_dir, monkeypatch):
        provider = self._setup_pod(monkeypatch, status="RUNNING")
        runner = self._setup_runner(monkeypatch)

        resp = client.post("/pods/pod-a/review", json=self._body())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        job_id = data["job_id"]

        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done", final
        result = final["result"]
        assert result["pod_name"] == "pod-a"
        assert result["server_url"] == "https://pod-a.ts.net:9189"
        assert "deploy_result" in result and "review_result" in result

        # Running pod is NOT auto-started.
        provider.start_pod.assert_not_called()

        # Three HTTP calls: idempotency-check info, deploy, review.
        calls = runner._request.call_args_list
        assert calls[0].args == ("GET", "/main/info")
        assert calls[1].args == ("POST", "/main/deploy")
        assert calls[2].args == ("POST", "/reviews/local")
        deploy_body = calls[1].kwargs["json"]
        assert deploy_body["pr"] == 99
        assert deploy_body["repo"] == "https://github.com/comfy-org/ComfyUI"
        review_body = calls[2].kwargs["json"]
        assert review_body == {
            "install": "main", "owner": "comfy-org",
            "repo": "ComfyUI", "pr": 99,
        }

    def test_auto_wakes_stopped_pod(self, client, tmp_config_dir, monkeypatch):
        provider = self._setup_pod(monkeypatch, status="STOPPED")
        self._setup_runner(monkeypatch)

        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done", final
        # Stopped pod was started.
        provider.start_pod.assert_called_once_with("pod-id-123")

    def test_terminated_pod_fails(self, client, tmp_config_dir, monkeypatch):
        provider = self._setup_pod(monkeypatch, status="TERMINATED")
        # No runner needed — we should fail before any HTTP call.

        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "error"
        assert "terminated" in final["error"].lower()

    def test_missing_pod_404(self, client, tmp_config_dir, monkeypatch):
        from comfy_runner.hosted import config as hcfg
        monkeypatch.setattr(
            hcfg, "get_pod_record", lambda provider, name: None,
        )
        resp = client.post("/pods/pod-a/review", json=self._body())
        assert resp.status_code == 404

    def test_invalid_pod_name_rejected(self, client, tmp_config_dir):
        resp = client.post("/pods/..%2Fevil/review", json=self._body())
        # _validate_pod_name rejects ``..`` etc; flask URL decoding may
        # serve a 404 if the route doesn't match. Either is acceptable;
        # the key is we never reach the worker.
        assert resp.status_code in (400, 404)

    def test_install_validated(self, client, tmp_config_dir, monkeypatch):
        self._setup_pod(monkeypatch)
        resp = client.post(
            "/pods/pod-a/review", json=self._body(install=".."),
        )
        assert resp.status_code == 400

    def test_extras_passed_through(self, client, tmp_config_dir, monkeypatch):
        self._setup_pod(monkeypatch)
        runner = self._setup_runner(monkeypatch)
        body = self._body(
            extra_workflows=["https://h/wf.json"],
            extra_models=[{"name": "m", "url": "https://h/m",
                           "directory": "loras"}],
            github_token="ghp",
            download_token="hf",
            allow_arbitrary_urls=True,
            skip_provisioning=True,
        )
        resp = client.post("/pods/pod-a/review", json=body)
        job_id = resp.get_json()["job_id"]
        _wait_job(client, job_id, timeout=5)

        # Index [2] now: [0]=info (idempotency), [1]=deploy, [2]=review.
        review_body = runner._request.call_args_list[2].kwargs["json"]
        assert review_body["github_token"] == "ghp"
        assert review_body["download_token"] == "hf"
        assert review_body["allow_arbitrary_urls"] is True
        assert review_body["skip_provisioning"] is True
        assert review_body["extra_workflows"] == ["https://h/wf.json"]
        assert review_body["extra_models"] == [
            {"name": "m", "url": "https://h/m", "directory": "loras"}
        ]

    # ── Purpose gating ────────────────────────────────────────────────

    def test_test_purpose_pod_refused(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="test")
        # Don't set up the runner — we should fail before any HTTP call.
        resp = client.post("/pods/pod-a/review", json=self._body())
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["ok"] is False
        assert "test" in data["error"]
        assert "force_purpose" in data["error"]

    def test_test_purpose_pod_allowed_with_force(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="test")
        self._setup_runner(monkeypatch)
        resp = client.post(
            "/pods/pod-a/review",
            json=self._body(force_purpose=True),
        )
        assert resp.status_code == 200
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        assert final["result"]["pod_purpose"] == "test"
        # Force-purpose warning was surfaced.
        assert any(
            "force_purpose" in line for line in final.get("output", [])
        )

    def test_pr_purpose_pod_allowed_silently(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="pr")
        self._setup_runner(monkeypatch)
        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        assert final["result"]["pod_purpose"] == "pr"
        # No purpose-warning lines for ``pr`` pods.
        for line in final.get("output", []):
            assert "purpose='persistent'" not in line
            assert "purpose='test'" not in line

    def test_persistent_purpose_pod_warns_but_allows(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="persistent")
        self._setup_runner(monkeypatch)
        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        assert final["result"]["pod_purpose"] == "persistent"
        # The warning text must appear in the job output.
        assert any(
            "persistent" in line and "general-dev" in line
            for line in final.get("output", [])
        )

    def test_missing_purpose_treated_as_persistent(
        self, client, tmp_config_dir, monkeypatch,
    ):
        # Pre-existing pods without an explicit ``purpose`` field default
        # to persistent for safety.
        self._setup_pod(monkeypatch, purpose=None)
        self._setup_runner(monkeypatch)
        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        assert final["result"]["pod_purpose"] == "persistent"

    # ── Idempotent re-deploy (item 4) ──────────────────────────────────

    def test_idempotent_skip_when_pr_already_deployed(
        self, client, tmp_config_dir, monkeypatch,
    ):
        # Pod's installation already reports deployed_pr=99; default
        # behavior must skip the deploy step.
        self._setup_pod(monkeypatch, purpose="pr")
        runner = self._setup_runner(
            monkeypatch,
            deployed_pr_on_pod=99,
            deployed_repo_on_pod="https://github.com/comfy-org/ComfyUI",
        )
        # Override side_effect: only info + review (no deploy resp).
        runner._request = MagicMock(side_effect=[
            {
                "ok": True, "name": "main",
                "deployed_pr": 99,
                "deployed_repo": "https://github.com/comfy-org/ComfyUI",
            },
            {"ok": True, "job_id": "review-1"},
        ])
        runner.poll_job = MagicMock(side_effect=[
            # No deploy poll — only review poll.
            {
                "manifest": None, "resolved": None,
                "downloaded": [], "skipped": [], "failed": [], "errors": [],
                "workflows": [], "workflows_dir": "/x", "failures": [],
            },
        ])

        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done", final

        # Two HTTP calls: info, then review (deploy was skipped).
        calls = runner._request.call_args_list
        assert len(calls) == 2
        assert calls[0].args == ("GET", "/main/info")
        assert calls[1].args == ("POST", "/reviews/local")

        # Deploy result reflects the skip.
        assert final["result"]["deploy_result"] == {
            "skipped": True, "reason": "PR already deployed",
        }
        # Visible in output.
        assert any(
            "already deployed" in line for line in final.get("output", [])
        )

    def test_idempotent_repo_normalization(
        self, client, tmp_config_dir, monkeypatch,
    ):
        # ``Comfy-Org/ComfyUI`` and ``comfy-org/comfyui`` normalize to
        # the same value; the idempotency check should treat them as a
        # match.
        self._setup_pod(monkeypatch, purpose="pr")
        runner = self._setup_runner(
            monkeypatch,
            deployed_pr_on_pod=99,
            deployed_repo_on_pod="github.com/Comfy-Org/ComfyUI.git",
        )
        runner._request = MagicMock(side_effect=[
            {
                "ok": True,
                "deployed_pr": 99,
                "deployed_repo": "github.com/Comfy-Org/ComfyUI.git",
            },
            {"ok": True, "job_id": "review-1"},
        ])
        runner.poll_job = MagicMock(return_value={
            "manifest": None, "resolved": None,
            "downloaded": [], "skipped": [], "failed": [], "errors": [],
            "workflows": [], "workflows_dir": "/x", "failures": [],
        })
        resp = client.post(
            "/pods/pod-a/review",
            json=self._body(owner="comfy-org", repo="comfyui"),
        )
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        # Deploy was skipped despite case/.git differences.
        assert final["result"]["deploy_result"]["skipped"] is True

    def test_force_deploy_overrides_idempotency(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="pr")
        runner = self._setup_runner(
            monkeypatch,
            deployed_pr_on_pod=99,
            deployed_repo_on_pod="https://github.com/comfy-org/ComfyUI",
        )
        # With force_deploy, the worker should NOT consult /info — it
        # goes straight to deploy. So only 2 calls expected: deploy, review.
        runner._request = MagicMock(side_effect=[
            {"ok": True, "job_id": "deploy-1"},
            {"ok": True, "job_id": "review-1"},
        ])
        runner.poll_job = MagicMock(side_effect=[
            {"restarted": True},
            {
                "manifest": None, "resolved": None,
                "downloaded": [], "skipped": [], "failed": [], "errors": [],
                "workflows": [], "workflows_dir": "/x", "failures": [],
            },
        ])

        resp = client.post(
            "/pods/pod-a/review",
            json=self._body(force_deploy=True),
        )
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        # No GET /info call — went straight to deploy.
        calls = runner._request.call_args_list
        assert calls[0].args == ("POST", "/main/deploy")
        # Real deploy result, not skip marker.
        assert final["result"]["deploy_result"] == {"restarted": True}

    def test_idempotency_falls_back_on_info_error(
        self, client, tmp_config_dir, monkeypatch,
    ):
        # If GET /<install>/info fails for any reason, fall back to a
        # full deploy rather than skipping incorrectly.
        self._setup_pod(monkeypatch, purpose="pr")
        runner = self._setup_runner(monkeypatch)
        runner._request = MagicMock(side_effect=[
            RuntimeError("info call failed"),  # GET /main/info
            {"ok": True, "job_id": "deploy-1"},
            {"ok": True, "job_id": "review-1"},
        ])
        runner.poll_job = MagicMock(side_effect=[
            {"restarted": True},
            {
                "manifest": None, "resolved": None,
                "downloaded": [], "skipped": [], "failed": [], "errors": [],
                "workflows": [], "workflows_dir": "/x", "failures": [],
            },
        ])
        resp = client.post("/pods/pod-a/review", json=self._body())
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        # Deploy was performed normally.
        assert final["result"]["deploy_result"] == {"restarted": True}

    # ── idle_timeout_s override (item 4) ───────────────────────────────

    def test_idle_timeout_override_updates_pod_record(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="pr")
        self._setup_runner(monkeypatch)

        updated: dict = {}

        def fake_update(provider, name, fn):
            rec = {"id": "id", "purpose": "pr", "idle_timeout_s": 1800}
            new_rec = fn(rec)
            updated.update(new_rec)
        monkeypatch.setattr(
            "comfy_runner.hosted.config.update_pod_record", fake_update,
        )

        resp = client.post(
            "/pods/pod-a/review",
            json=self._body(idle_timeout_s=900),
        )
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        assert final["result"]["idle_timeout_s"] == 900
        assert updated["idle_timeout_s"] == 900
        # Visible in output.
        assert any(
            "idle timeout updated" in line.lower()
            for line in final.get("output", [])
        )

    def test_idle_timeout_validation(self, client, tmp_config_dir, monkeypatch):
        self._setup_pod(monkeypatch, purpose="pr")
        for bad in (-1, 0, "x", True):
            resp = client.post(
                "/pods/pod-a/review",
                json=self._body(idle_timeout_s=bad),
            )
            assert resp.status_code == 400, bad

    # ── skip_deploy (item 3 — runpod target uses launch-pr first) ─────

    def test_skip_deploy_omits_deploy_step(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._setup_pod(monkeypatch, purpose="pr")
        runner = MagicMock()
        # Only one HTTP call expected: /reviews/local. No deploy.
        runner._request = MagicMock(return_value={
            "ok": True, "job_id": "review-1",
        })
        runner.poll_job = MagicMock(return_value={
            "manifest": None, "resolved": None,
            "downloaded": [], "skipped": [], "failed": [], "errors": [],
            "workflows": [], "workflows_dir": "/x", "failures": [],
        })
        monkeypatch.setattr(
            "comfy_runner.hosted.remote.RemoteRunner",
            lambda url: runner,
        )
        resp = client.post(
            "/pods/pod-a/review", json=self._body(skip_deploy=True),
        )
        job_id = resp.get_json()["job_id"]
        final = _wait_job(client, job_id, timeout=5)
        assert final["status"] == "done"
        # Exactly one HTTP call, and it was /reviews/local.
        runner._request.assert_called_once()
        assert runner._request.call_args.args == ("POST", "/reviews/local")
        # deploy_result is None when deploy was skipped.
        assert final["result"]["deploy_result"] is None
        # Visible in job output too.
        assert any(
            "Skipping deploy" in line for line in final.get("output", [])
        )


# =====================================================================
# POST /reviews/cleanup — terminate ephemeral PR pods (item 3)
# =====================================================================


class TestReviewsCleanup:
    def _stub_provider(self, monkeypatch, *, terminate_raises=None):
        provider = MagicMock()
        if terminate_raises:
            provider.terminate_pod = MagicMock(side_effect=terminate_raises)
        else:
            provider.terminate_pod = MagicMock()
        monkeypatch.setattr(
            "comfy_runner_server.server._get_runpod_provider",
            lambda: provider,
        )
        return provider

    def _stub_records(self, monkeypatch, records: dict):
        from comfy_runner.hosted import config as hcfg

        def list_pod_records(_provider):
            return dict(records)

        removed: list[str] = []

        def remove_pod_record(_provider, name):
            removed.append(name)
            return name in records
        monkeypatch.setattr(hcfg, "list_pod_records", list_pod_records)
        monkeypatch.setattr(hcfg, "remove_pod_record", remove_pod_record)
        return removed

    def test_terminates_only_matching_pr_pods(
        self, client, tmp_config_dir, monkeypatch,
    ):
        provider = self._stub_provider(monkeypatch)
        removed = self._stub_records(monkeypatch, {
            "pr-foo-42": {"id": "id-42", "purpose": "pr", "pr_number": 42},
            "pr-foo-99": {"id": "id-99", "purpose": "pr", "pr_number": 99},
            "dev-box":   {"id": "id-d",  "purpose": "persistent"},
            "test-rig":  {"id": "id-t",  "purpose": "test", "pr_number": 42},
        })
        resp = client.post("/reviews/cleanup", json={"pr": 42})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["total_found"] == 1
        assert data["total_terminated"] == 1
        assert data["terminated"] == [{"name": "pr-foo-42", "id": "id-42"}]
        assert data["removed_records"] == ["pr-foo-42"]
        # Only the matching pr-#42 pod was terminated.
        provider.terminate_pod.assert_called_once_with("id-42")
        # Persistent + test pods + non-matching PR pods were untouched.
        assert removed == ["pr-foo-42"]

    def test_dry_run_lists_without_terminating(
        self, client, tmp_config_dir, monkeypatch,
    ):
        provider = self._stub_provider(monkeypatch)
        self._stub_records(monkeypatch, {
            "pr-foo-42": {"id": "id-42", "purpose": "pr", "pr_number": 42},
        })
        resp = client.post(
            "/reviews/cleanup", json={"pr": 42, "dry_run": True},
        )
        data = resp.get_json()
        assert data["dry_run"] is True
        assert data["total_found"] == 1
        assert data["total_terminated"] == 0
        assert data["skipped"] == [
            {"name": "pr-foo-42", "id": "id-42", "reason": "dry-run"}
        ]
        provider.terminate_pod.assert_not_called()

    def test_no_matches_returns_zero(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._stub_provider(monkeypatch)
        self._stub_records(monkeypatch, {
            "pr-foo-7": {"id": "id-7", "purpose": "pr", "pr_number": 7},
        })
        resp = client.post("/reviews/cleanup", json={"pr": 999})
        data = resp.get_json()
        assert data["total_found"] == 0
        assert data["terminated"] == []

    def test_invalid_pr_rejected(self, client, tmp_config_dir):
        for bad in (None, "x", -1, 0, True):
            resp = client.post(
                "/reviews/cleanup", json={"pr": bad},
            )
            assert resp.status_code == 400, bad

    def test_terminate_failure_collected_as_skipped(
        self, client, tmp_config_dir, monkeypatch,
    ):
        self._stub_provider(
            monkeypatch, terminate_raises=RuntimeError("API timeout"),
        )
        self._stub_records(monkeypatch, {
            "pr-foo-42": {"id": "id-42", "purpose": "pr", "pr_number": 42},
        })
        resp = client.post("/reviews/cleanup", json={"pr": 42})
        data = resp.get_json()
        assert data["total_terminated"] == 0
        assert len(data["skipped"]) == 1
        assert "API timeout" in data["skipped"][0]["error"]


# =====================================================================
# OpenAPI spec contains the new routes
# =====================================================================


class TestOpenAPIIncludesReviewRoutes:
    def test_spec_contains_reviews_local(self, client):
        resp = client.get("/openapi.json")
        spec = resp.get_json()
        assert "/reviews/local" in spec["paths"]
        assert "post" in spec["paths"]["/reviews/local"]

    def test_spec_contains_pods_review(self, client):
        resp = client.get("/openapi.json")
        spec = resp.get_json()
        assert "/pods/{name}/review" in spec["paths"]
        assert "post" in spec["paths"]["/pods/{name}/review"]

    def test_spec_contains_reviews_cleanup(self, client):
        resp = client.get("/openapi.json")
        spec = resp.get_json()
        assert "/reviews/cleanup" in spec["paths"]
        assert "post" in spec["paths"]["/reviews/cleanup"]
