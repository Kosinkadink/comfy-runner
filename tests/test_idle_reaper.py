"""Tests for the PR-pod idle reaper, activity tracking, and /pods/launch-pr."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure comfy-runner root is on sys.path
_RUNNER_ROOT = Path(__file__).resolve().parent.parent
if str(_RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNNER_ROOT))

from comfy_runner.hosted.config import (
    get_pod_record,
    list_pod_records,
    set_pod_record,
)
from comfy_runner.hosted.provider import PodInfo
from comfy_runner_server import server as srv


def _make_pod(**overrides) -> PodInfo:
    defaults = dict(
        id="pod_xyz", name="pr-42", status="RUNNING",
        gpu_type="NVIDIA L40S", datacenter="US-KS-2",
        cost_per_hr=0.74, image="runpod/ubuntu:24.04", raw={},
    )
    defaults.update(overrides)
    return PodInfo(**defaults)


# =====================================================================
# Activity helpers
# =====================================================================

class TestTouchPodActivity:
    def test_no_record_is_noop(self, tmp_config_dir):
        srv._touch_pod_activity("nope")  # should not raise
        assert get_pod_record("runpod", "nope") is None

    def test_stamps_last_active_at(self, tmp_config_dir):
        set_pod_record("runpod", "p", {"id": "x", "purpose": "pr"})
        before = int(time.time())
        srv._touch_pod_activity("p")
        rec = get_pod_record("runpod", "p")
        assert rec["last_active_at"] >= before

    def test_clears_stopped_idle_hint(self, tmp_config_dir):
        set_pod_record("runpod", "p", {
            "id": "x", "purpose": "pr",
            "status_hint": "stopped_idle",
        })
        srv._touch_pod_activity("p")
        rec = get_pod_record("runpod", "p")
        assert "status_hint" not in rec

    def test_clears_other_status_hints(self, tmp_config_dir):
        """Any reaper-stamped hint (e.g. ``exited``) should be wiped on touch."""
        for hint in ("exited", "terminated", "anything"):
            set_pod_record("runpod", "p", {
                "id": "x", "purpose": "pr", "status_hint": hint,
            })
            srv._touch_pod_activity("p")
            assert "status_hint" not in get_pod_record("runpod", "p")


class TestIdleSecondsRemaining:
    def test_non_pr_pod_returns_none(self):
        assert srv._idle_seconds_remaining({"id": "x"}) is None
        assert srv._idle_seconds_remaining({"purpose": "persistent"}) is None

    def test_no_last_active_returns_full_timeout(self):
        rec = {"purpose": "pr", "idle_timeout_s": 300}
        assert srv._idle_seconds_remaining(rec) == 300

    def test_default_timeout(self):
        rec = {"purpose": "pr"}
        assert srv._idle_seconds_remaining(rec) == srv.DEFAULT_IDLE_TIMEOUT_S

    def test_recent_activity_returns_remaining(self):
        rec = {"purpose": "pr", "idle_timeout_s": 600,
               "last_active_at": int(time.time()) - 100}
        remaining = srv._idle_seconds_remaining(rec)
        assert 490 <= remaining <= 510

    def test_stale_returns_zero(self):
        rec = {"purpose": "pr", "idle_timeout_s": 60,
               "last_active_at": int(time.time()) - 600}
        assert srv._idle_seconds_remaining(rec) == 0


# =====================================================================
# Reaper iteration
# =====================================================================

class TestIdleReaperIteration:
    def _patch_provider(self, **methods):
        """Patch _get_runpod_provider to return a MagicMock with given methods."""
        provider = MagicMock()
        for k, v in methods.items():
            setattr(provider, k, v)
        return patch.object(srv, "_get_runpod_provider", return_value=provider)

    def test_no_records_no_action(self, tmp_config_dir):
        with self._patch_provider():
            summary = srv._idle_reaper_iteration()
        assert summary["checked"] == 0
        assert summary["stopped"] == []

    def test_provider_unavailable_returns_empty(self, tmp_config_dir):
        with patch.object(srv, "_get_runpod_provider",
                          side_effect=RuntimeError("no api key")):
            summary = srv._idle_reaper_iteration()
        assert summary["checked"] == 0
        assert summary["stopped"] == []

    def test_skips_non_pr_pods(self, tmp_config_dir):
        set_pod_record("runpod", "persistent-pod", {
            "id": "p1", "purpose": "persistent",
            "last_active_at": int(time.time()) - 9999,
        })
        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(id="p1")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._idle_reaper_iteration()
        assert summary["checked"] == 0
        assert summary["stopped"] == []
        provider.stop_pod.assert_not_called()

    def test_skips_active_pr_pods(self, tmp_config_dir):
        set_pod_record("runpod", "pr-1", {
            "id": "p1", "purpose": "pr", "idle_timeout_s": 600,
            "last_active_at": int(time.time()),
        })
        provider = MagicMock()
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._idle_reaper_iteration()
        assert summary["checked"] == 1
        assert summary["stopped"] == []
        provider.stop_pod.assert_not_called()

    def test_stops_idle_pr_pod(self, tmp_config_dir):
        set_pod_record("runpod", "pr-2", {
            "id": "p2", "purpose": "pr", "idle_timeout_s": 60,
            "last_active_at": int(time.time()) - 9999,
        })
        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(id="p2", status="RUNNING")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._idle_reaper_iteration()
        assert summary["stopped"] == [{"name": "pr-2", "id": "p2"}]
        provider.stop_pod.assert_called_once_with("p2")
        rec = get_pod_record("runpod", "pr-2")
        assert rec["status_hint"] == "stopped_idle"
        assert "stopped_at" in rec

    def test_skips_pod_already_not_running(self, tmp_config_dir):
        set_pod_record("runpod", "pr-3", {
            "id": "p3", "purpose": "pr", "idle_timeout_s": 60,
            "last_active_at": int(time.time()) - 9999,
        })
        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(id="p3", status="EXITED")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._idle_reaper_iteration()
        assert summary["stopped"] == []
        provider.stop_pod.assert_not_called()

    def test_skips_busy_pod(self, tmp_config_dir):
        set_pod_record("runpod", "pr-4", {
            "id": "p4", "purpose": "pr", "idle_timeout_s": 60,
            "last_active_at": int(time.time()) - 9999,
        })
        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(id="p4", status="RUNNING")
        # Hold the pod lock — simulating an in-flight operation.
        lock = srv._get_pod_lock("pr-4")
        lock.acquire()
        try:
            with patch.object(srv, "_get_runpod_provider", return_value=provider):
                summary = srv._idle_reaper_iteration()
        finally:
            lock.release()
        assert summary["stopped"] == []
        assert any(s["reason"] == "busy" for s in summary["skipped"])
        provider.stop_pod.assert_not_called()


# =====================================================================
# HTTP routes
# =====================================================================

@pytest.fixture()
def client(tmp_config_dir):
    """Flask test client."""
    from comfy_runner_server.server import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


class TestRouteTouch:
    def test_404_on_unknown_pod(self, client, tmp_config_dir):
        resp = client.post("/pods/missing/touch")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_touches_existing_pod(self, client, tmp_config_dir):
        set_pod_record("runpod", "pr-9", {
            "id": "pid9", "purpose": "pr", "idle_timeout_s": 600,
        })
        resp = client.post("/pods/pr-9/touch")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["name"] == "pr-9"
        assert isinstance(body["last_active_at"], int)
        rec = get_pod_record("runpod", "pr-9")
        assert rec["last_active_at"] == body["last_active_at"]


def _wait_for_job(job_id: str, timeout_s: float = 5.0) -> dict:
    """Wait for an async job to leave the 'running' state and return it.

    Raises ``AssertionError`` if the job hasn't completed within
    *timeout_s*; a fast deterministic failure rather than a flaky pass.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = srv._jobs.get(job_id)
        if job and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not complete within {timeout_s}s")


class TestRouteLaunchPr:
    def test_requires_pr(self, client, tmp_config_dir):
        resp = client.post("/pods/launch-pr", json={})
        assert resp.status_code == 400
        assert "'pr'" in resp.get_json()["error"]

    def test_pr_must_be_int(self, client, tmp_config_dir):
        resp = client.post("/pods/launch-pr", json={"pr": "abc"})
        assert resp.status_code == 400

    def test_idle_timeout_must_be_positive(self, client, tmp_config_dir):
        resp = client.post(
            "/pods/launch-pr",
            json={"pr": 5, "idle_timeout_s": 0},
        )
        assert resp.status_code == 400

    def test_derives_pod_name_no_repo(self, client, tmp_config_dir):
        # Fail the job quickly so we can inspect what it tried to do.
        provider = MagicMock()
        provider.create_pod.side_effect = RuntimeError("stub")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.post("/pods/launch-pr", json={"pr": 7})
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["ok"] is True
            assert body["name"] == "pr-7"
            _wait_for_job(body["job_id"])

    def test_derives_pod_name_with_repo(self, client, tmp_config_dir):
        provider = MagicMock()
        provider.create_pod.side_effect = RuntimeError("stub")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.post("/pods/launch-pr", json={
                "pr": 1234,
                "repo": "https://github.com/comfy-org/ComfyUI.git",
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["name"] == "pr-comfy-org-comfyui-1234"
            _wait_for_job(body["job_id"])

    def test_records_purpose_and_pr(self, client, tmp_config_dir):
        """Verify that even when later steps fail, the record is created
        with purpose='pr', pr_number, idle_timeout_s, and last_active_at
        (so the reaper can still clean up failed launches)."""
        provider = MagicMock()
        # create_pod succeeds; subsequent _wait_for_pod_server raises so the
        # job ends without needing real network.
        provider.create_pod.return_value = _make_pod(
            id="newpod", name="pr-42", status="RUNNING",
        )

        with patch.object(srv, "_get_runpod_provider", return_value=provider), \
             patch.object(srv, "_wait_for_pod_server",
                          side_effect=RuntimeError("stub-wait")):
            before = int(time.time())
            resp = client.post("/pods/launch-pr", json={
                "pr": 42,
                "idle_timeout_s": 120,
            })
            assert resp.status_code == 200
            _wait_for_job(resp.get_json()["job_id"])

            rec = get_pod_record("runpod", "pr-42")
            assert rec is not None
            assert rec["purpose"] == "pr"
            assert rec["pr_number"] == 42
            assert rec["idle_timeout_s"] == 120
            assert rec["id"] == "newpod"
            # Activity must be stamped on the initial record so the
            # reaper can sweep up an orphan whose deploy failed.
            assert rec["last_active_at"] >= before

    def test_dotted_repo_does_not_collide_with_dashed(self, client, tmp_config_dir):
        """``owner/my.repo`` and ``owner/my-repo`` must yield distinct slugs."""
        provider = MagicMock()
        provider.create_pod.side_effect = RuntimeError("stub")
        names = []
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            for repo in ("owner/my.repo", "owner/my-repo"):
                resp = client.post("/pods/launch-pr", json={"pr": 1, "repo": repo})
                assert resp.status_code == 200
                body = resp.get_json()
                names.append(body["name"])
                _wait_for_job(body["job_id"])
        assert names == ["pr-owner-my_repo-1", "pr-owner-my-repo-1"]
        assert names[0] != names[1]

    def test_full_success_flow(self, client, tmp_config_dir):
        """End-to-end happy path with all remote calls mocked."""
        provider = MagicMock()
        provider.create_pod.return_value = _make_pod(
            id="happy_pod", name="pr-100", status="RUNNING",
        )

        # Patched ``_wait_for_pod_server`` returns a fake URL so the
        # deploy step has something to send to.
        fake_url = "http://comfy-pr-100.example.ts.net:9189"

        # Stub the RemoteRunner used inside the worker so it doesn't
        # actually open HTTP connections.
        fake_runner = MagicMock()
        fake_runner._request.return_value = {"job_id": "remote-job-1"}
        fake_runner.poll_job.return_value = {"status": "done", "ok": True}

        with patch.object(srv, "_get_runpod_provider", return_value=provider), \
             patch.object(srv, "_wait_for_pod_server", return_value=fake_url), \
             patch("comfy_runner.hosted.remote.RemoteRunner",
                   return_value=fake_runner):
            resp = client.post("/pods/launch-pr", json={
                "pr": 100,
                "repo": "comfy-org/ComfyUI",
                "idle_timeout_s": 300,
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["name"] == "pr-comfy-org-comfyui-100"
            job = _wait_for_job(body["job_id"])

        # Job finished successfully.
        assert job["status"] == "done", f"job error: {job.get('error')}"
        result = job["result"]
        assert result["pr"] == 100
        assert result["created"] is True
        assert result["server_url"] == fake_url
        assert result["comfy_url"] == "http://comfy-pr-100.example.ts.net:8188"
        assert result["idle_timeout_s"] == 300
        assert result["deploy_result"] == {"status": "done", "ok": True}

        # Record persisted with all metadata.
        rec = get_pod_record("runpod", "pr-comfy-org-comfyui-100")
        assert rec is not None
        assert rec["purpose"] == "pr"
        assert rec["pr_number"] == 100
        assert rec["idle_timeout_s"] == 300
        assert rec["id"] == "happy_pod"
        assert rec["last_active_at"] > 0

        # The deploy was forwarded with the right body.
        fake_runner._request.assert_called_once()
        method, path = fake_runner._request.call_args.args
        deploy_body = fake_runner._request.call_args.kwargs["json"]
        assert method == "POST"
        assert path == "/main/deploy"
        assert deploy_body["pr"] == 100
        assert deploy_body["repo"] == "comfy-org/ComfyUI"
        assert deploy_body["start"] is True

    def test_wake_clears_stopped_idle_hint(self, client, tmp_config_dir):
        """Re-launching a sleeping pod should clear ``status_hint``."""
        # Pre-existing record marked stopped_idle by an earlier reaper sweep.
        set_pod_record("runpod", "pr-comfy-org-comfyui-200", {
            "id": "sleeping_pod",
            "purpose": "pr",
            "pr_number": 200,
            "idle_timeout_s": 600,
            "status_hint": "stopped_idle",
            "last_active_at": int(time.time()) - 9999,
            "gpu_type": "NVIDIA L40S",
            "datacenter": "US-KS-2",
            "image": "runpod/ubuntu:24.04",
        })

        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(
            id="sleeping_pod", status="EXITED",
        )
        # The launcher should fail at _wait_for_pod_server (we don't care);
        # we only need to verify the metadata refresh path runs.
        with patch.object(srv, "_get_runpod_provider", return_value=provider), \
             patch.object(srv, "_wait_for_pod_server",
                          side_effect=RuntimeError("stub-wait")):
            resp = client.post("/pods/launch-pr", json={
                "pr": 200,
                "repo": "comfy-org/ComfyUI",
            })
            assert resp.status_code == 200
            _wait_for_job(resp.get_json()["job_id"])

        # status_hint cleared, last_active_at refreshed, start_pod called.
        rec = get_pod_record("runpod", "pr-comfy-org-comfyui-200")
        assert "status_hint" not in rec
        provider.start_pod.assert_called_once_with("sleeping_pod")


class TestPodsListExposesActivity:
    def test_running_pr_pod_exposes_idle_in_s(self, client, tmp_config_dir):
        now = int(time.time())
        set_pod_record("runpod", "pr-99", {
            "id": "pp99", "purpose": "pr", "pr_number": 99,
            "idle_timeout_s": 600, "last_active_at": now - 30,
            "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.list_pods.return_value = [
            _make_pod(id="pp99", name="pr-99", status="RUNNING"),
        ]
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.get("/pods")
            assert resp.status_code == 200
            data = resp.get_json()
            entry = next(p for p in data["pods"] if p["name"] == "pr-99")
            assert entry["purpose"] == "pr"
            assert entry["pr_number"] == 99
            assert entry["idle_timeout_s"] == 600
            assert entry["last_active_at"] == now - 30
            assert 560 <= entry["idle_in_s"] <= 580

    def test_purpose_filter_buckets(self, client, tmp_config_dir):
        """``?purpose=`` filters records by their stored purpose."""
        # Three buckets: pr / persistent / test
        set_pod_record("runpod", "pr-1", {
            "id": "p1", "purpose": "pr", "pr_number": 1,
            "gpu_type": "NVIDIA L40S",
        })
        set_pod_record("runpod", "persist-1", {
            "id": "p2", "purpose": "persistent",
            "gpu_type": "NVIDIA L40S",
        })
        set_pod_record("runpod", "test-1", {
            "id": "p3", "purpose": "test",
            "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.list_pods.return_value = [
            _make_pod(id="p1", name="pr-1", status="RUNNING"),
            _make_pod(id="p2", name="persist-1", status="RUNNING"),
            _make_pod(id="p3", name="test-1", status="RUNNING"),
        ]
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            # No filter — all three.
            resp = client.get("/pods")
            assert resp.status_code == 200
            names = sorted(p["name"] for p in resp.get_json()["pods"])
            assert names == ["persist-1", "pr-1", "test-1"]

            for purpose, expected in [
                ("pr", ["pr-1"]),
                ("persistent", ["persist-1"]),
                ("test", ["test-1"]),
            ]:
                resp = client.get(f"/pods?purpose={purpose}")
                assert resp.status_code == 200
                names = sorted(p["name"] for p in resp.get_json()["pods"])
                assert names == expected, (
                    f"?purpose={purpose} returned {names}, expected {expected}"
                )

    def test_purpose_filter_treats_missing_as_persistent(
        self, client, tmp_config_dir,
    ):
        """A record with no ``purpose`` key matches ``?purpose=persistent``."""
        set_pod_record("runpod", "legacy", {
            "id": "lg1", "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.list_pods.return_value = [
            _make_pod(id="lg1", name="legacy", status="RUNNING"),
        ]
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.get("/pods?purpose=persistent")
            names = [p["name"] for p in resp.get_json()["pods"]]
            assert names == ["legacy"]
            resp = client.get("/pods?purpose=pr")
            assert resp.get_json()["pods"] == []

    def test_stopped_idle_hint_surfaces(self, client, tmp_config_dir):
        set_pod_record("runpod", "pr-5", {
            "id": "pp5", "purpose": "pr", "pr_number": 5,
            "idle_timeout_s": 60, "last_active_at": int(time.time()) - 9999,
            "status_hint": "stopped_idle", "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.list_pods.return_value = [
            _make_pod(id="pp5", name="pr-5", status="EXITED"),
        ]
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.get("/pods")
            entry = next(p for p in resp.get_json()["pods"] if p["name"] == "pr-5")
            assert entry["status_hint"] == "stopped_idle"
            # Non-RUNNING pods don't get an idle_in_s field.
            assert "idle_in_s" not in entry


# =====================================================================
# /pods/create defaults purpose to "persistent"
# =====================================================================

class TestRoutePodsCreatePurpose:
    def test_default_purpose_is_persistent(self, client, tmp_config_dir):
        provider = MagicMock()
        provider.create_pod.return_value = _make_pod(
            id="newp", name="custom-pod", status="RUNNING",
        )
        with patch.object(srv, "_get_runpod_provider", return_value=provider), \
             patch.object(srv, "_wait_for_pod_server", return_value="http://x:9189"):
            resp = client.post("/pods/create", json={
                "name": "custom-pod",
                "wait_ready": False,
            })
            assert resp.status_code == 200
            _wait_for_job(resp.get_json()["job_id"])
        rec = get_pod_record("runpod", "custom-pod")
        assert rec is not None
        assert rec["purpose"] == "persistent"

    def test_body_purpose_overrides(self, client, tmp_config_dir):
        provider = MagicMock()
        provider.create_pod.return_value = _make_pod(
            id="custp", name="explicit-pod", status="RUNNING",
        )
        with patch.object(srv, "_get_runpod_provider", return_value=provider), \
             patch.object(srv, "_wait_for_pod_server", return_value="http://x:9189"):
            resp = client.post("/pods/create", json={
                "name": "explicit-pod",
                "wait_ready": False,
                "purpose": "test",
            })
            assert resp.status_code == 200
            _wait_for_job(resp.get_json()["job_id"])
        rec = get_pod_record("runpod", "explicit-pod")
        assert rec["purpose"] == "test"

    def test_invalid_purpose_is_rejected(self, client, tmp_config_dir):
        """Anything outside the {pr, persistent, test} enum is a 400."""
        for bad in ("foo", "", None, 42, "PERSISTENT"):
            resp = client.post("/pods/create", json={
                "name": "rejecto",
                "wait_ready": False,
                "purpose": bad,
            })
            assert resp.status_code == 400, (
                f"purpose={bad!r} should be rejected, got {resp.status_code}"
            )
            err = resp.get_json()["error"]
            assert "purpose" in err
        # Sanity: no record was ever created for any of the rejected attempts.
        assert get_pod_record("runpod", "rejecto") is None


# =====================================================================
# /pods/cleanup also removes records
# =====================================================================

class TestRoutePodsCleanupRemovesRecords:
    def test_terminate_removes_record(self, client, tmp_config_dir):
        set_pod_record("runpod", "test-abc", {
            "id": "tabc", "purpose": "test",
            "gpu_type": "NVIDIA L40S",
        })
        # An untracked test pod (no matching record) — record removal
        # should silently no-op for it.
        provider = MagicMock()
        provider.list_pods.return_value = [
            _make_pod(id="tabc", name="test-abc", status="RUNNING"),
            _make_pod(id="tdef", name="test-def", status="RUNNING"),
        ]
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.post("/pods/cleanup", json={"prefix": "test-"})
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["ok"] is True
            assert body["total_terminated"] == 2
            assert sorted(body["removed_records"]) == ["test-abc"]
        # Tracked record gone.
        assert get_pod_record("runpod", "test-abc") is None

    def test_dry_run_keeps_records(self, client, tmp_config_dir):
        set_pod_record("runpod", "test-keep", {
            "id": "tk", "purpose": "test", "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.list_pods.return_value = [
            _make_pod(id="tk", name="test-keep", status="RUNNING"),
        ]
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            resp = client.post("/pods/cleanup", json={
                "prefix": "test-", "dry_run": True,
            })
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["total_terminated"] == 0
            assert body["removed_records"] == []
        # Record preserved on dry run.
        assert get_pod_record("runpod", "test-keep") is not None
        provider.terminate_pod.assert_not_called()


# =====================================================================
# Watchdog dispatcher (_dispatch_on_overrun)
# =====================================================================

class TestDispatchOnOverrun:
    def test_none_no_op(self, tmp_config_dir):
        provider = MagicMock()
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._dispatch_on_overrun(
                {"kind": "remote", "pod_name": "any"}, "none",
            )
        assert summary["action"] == "none"
        provider.stop_pod.assert_not_called()
        provider.terminate_pod.assert_not_called()

    def test_no_pod_name_skips(self, tmp_config_dir):
        provider = MagicMock()
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._dispatch_on_overrun(
                {"kind": "local", "url": "http://x"}, "stop",
            )
        assert summary["action"] == "none"
        provider.stop_pod.assert_not_called()

    def test_stop_calls_stop_pod_when_running(self, tmp_config_dir):
        set_pod_record("runpod", "pr-overrun-1", {
            "id": "po1", "purpose": "pr", "pr_number": 5,
            "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(id="po1", status="RUNNING")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._dispatch_on_overrun(
                {"kind": "remote", "pod_name": "pr-overrun-1"}, "stop",
            )
        assert summary["action"] == "stopped"
        provider.stop_pod.assert_called_once_with("po1")
        rec = get_pod_record("runpod", "pr-overrun-1")
        assert rec["status_hint"] == "stopped_overrun"
        assert "stopped_at" in rec

    def test_stop_skips_when_not_running(self, tmp_config_dir):
        set_pod_record("runpod", "pr-overrun-2", {
            "id": "po2", "purpose": "pr", "pr_number": 6,
            "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        provider.get_pod.return_value = _make_pod(id="po2", status="EXITED")
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._dispatch_on_overrun(
                {"kind": "remote", "pod_name": "pr-overrun-2"}, "stop",
            )
        assert summary["action"] == "skipped"
        assert "not_running" in summary["reason"]
        provider.stop_pod.assert_not_called()
        # Record's status_hint NOT mutated when we skip.
        rec = get_pod_record("runpod", "pr-overrun-2")
        assert "status_hint" not in rec

    def test_stop_skips_when_pod_busy(self, tmp_config_dir):
        set_pod_record("runpod", "pr-overrun-3", {
            "id": "po3", "purpose": "pr", "pr_number": 7,
            "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        # Hold the pod lock to simulate a concurrent operation.
        lock = srv._get_pod_lock("pr-overrun-3")
        lock.acquire()
        try:
            with patch.object(srv, "_get_runpod_provider", return_value=provider):
                summary = srv._dispatch_on_overrun(
                    {"kind": "remote", "pod_name": "pr-overrun-3"}, "stop",
                )
        finally:
            lock.release()
        assert summary["action"] == "skipped"
        assert summary["reason"] == "pod_busy"
        provider.stop_pod.assert_not_called()

    def test_stop_falls_back_to_terminate_for_untracked(self, tmp_config_dir):
        # No record exists for this pod.
        provider = MagicMock()
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._dispatch_on_overrun(
                {"kind": "remote", "pod_name": "untracked-pod"}, "stop",
            )
        # Without a record there's nothing to terminate either.
        assert summary["action"] == "skipped"
        assert summary["reason"] == "no_record"
        provider.stop_pod.assert_not_called()

    def test_terminate_calls_terminate_and_removes_record(self, tmp_config_dir):
        set_pod_record("runpod", "test-overrun-1", {
            "id": "to1", "purpose": "test",
            "gpu_type": "NVIDIA L40S",
        })
        provider = MagicMock()
        with patch.object(srv, "_get_runpod_provider", return_value=provider):
            summary = srv._dispatch_on_overrun(
                {"kind": "runpod", "pod_name": "test-overrun-1"}, "terminate",
            )
        assert summary["action"] == "terminated"
        provider.terminate_pod.assert_called_once_with("to1")
        assert get_pod_record("runpod", "test-overrun-1") is None

    def test_default_per_kind(self):
        assert srv._default_on_overrun_for_kind("runpod") == "terminate"
        assert srv._default_on_overrun_for_kind("remote") == "stop"
        assert srv._default_on_overrun_for_kind("local") == "none"
        assert srv._default_on_overrun_for_kind("anything") == "none"

    def test_resolve_explicit_wins(self):
        body = {"kind": "runpod"}  # default would be terminate
        assert srv._resolve_on_overrun(body, "stop") == "stop"
        assert srv._resolve_on_overrun(body, None) == "terminate"
