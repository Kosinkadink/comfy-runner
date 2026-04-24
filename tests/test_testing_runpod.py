"""Tests for Phase 6 RunPod integration — runpod.py orchestration and CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comfy_runner.testing.runpod import RunPodTestConfig, RunPodTestResult, _wait_for_server


# ---------------------------------------------------------------------------
# _wait_for_server
# ---------------------------------------------------------------------------

class TestWaitForServer:
    def test_immediate_success(self):
        mock_resp = MagicMock(ok=True)
        mock_resp.json.return_value = {"ok": True, "system_info": {}}
        with patch("comfy_runner.testing.runpod.requests.get", return_value=mock_resp):
            _wait_for_server("http://fake:9189", timeout=5, poll_interval=1)

    def test_timeout(self):
        with patch("comfy_runner.testing.runpod.requests.get") as mock_get:
            import requests
            mock_get.side_effect = requests.ConnectionError("nope")
            with pytest.raises(RuntimeError, match="did not become ready"):
                _wait_for_server("http://fake:9189", timeout=2, poll_interval=1)

    def test_retries_until_ready(self):
        import requests
        mock_ok = MagicMock(ok=True)
        mock_ok.json.return_value = {"ok": True}
        responses = [
            requests.ConnectionError("nope"),
            requests.ConnectionError("nope"),
            mock_ok,
        ]
        with patch("comfy_runner.testing.runpod.requests.get", side_effect=responses):
            _wait_for_server("http://fake:9189", timeout=30, poll_interval=0.1)

    def test_rejects_proxy_html(self):
        """RunPod proxy returns 200 OK HTML before server is bound."""
        html_resp = MagicMock(ok=True)
        html_resp.json.side_effect = ValueError("No JSON")
        json_resp = MagicMock(ok=True)
        json_resp.json.return_value = {"ok": True}
        with patch("comfy_runner.testing.runpod.requests.get", side_effect=[html_resp, json_resp]):
            _wait_for_server("http://fake:9189", timeout=30, poll_interval=0.1)


# ---------------------------------------------------------------------------
# RunPodTestConfig
# ---------------------------------------------------------------------------

class TestRunPodTestConfig:
    def test_defaults(self):
        cfg = RunPodTestConfig(suite_path="/test/suite")
        assert cfg.timeout == 600
        assert cfg.terminate is True
        assert cfg.install_name == "main"
        assert cfg.gpu_type is None

    def test_custom(self):
        cfg = RunPodTestConfig(
            suite_path="/test/suite",
            gpu_type="A100",
            pr=42,
            terminate=False,
        )
        assert cfg.gpu_type == "A100"
        assert cfg.pr == 42
        assert cfg.terminate is False


# ---------------------------------------------------------------------------
# Helpers for run_on_runpod tests
# ---------------------------------------------------------------------------

def _make_suite(tmp_path: Path) -> Path:
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "suite.json").write_text(json.dumps({
        "name": "Test Suite",
        "description": "A test suite",
    }))
    wf_dir = suite_dir / "workflows"
    wf_dir.mkdir()
    (wf_dir / "wf1.json").write_text(json.dumps({
        "1": {"class_type": "KSampler", "inputs": {"seed": 0}},
    }))
    return suite_dir


# ---------------------------------------------------------------------------
# run_on_runpod (mocked)
# ---------------------------------------------------------------------------

class TestRunOnRunpod:
    @patch("comfy_runner.testing.runpod.RunPodProvider")
    @patch("comfy_runner.testing.runpod.RemoteRunner")
    @patch("comfy_runner.testing.runpod._wait_for_server")
    @patch("comfy_runner.testing.runpod.get_pod_record", return_value=None)
    @patch("comfy_runner.testing.runpod.set_pod_record")
    @patch("comfy_runner.testing.runpod.run_suite")
    @patch("comfy_runner.testing.runpod.build_report")
    @patch("comfy_runner.testing.runpod.write_report", return_value={})
    @patch("comfy_runner.testing.runpod.load_suite")
    def test_full_lifecycle(
        self, mock_load, mock_write, mock_build, mock_run,
        mock_set_rec, mock_get_rec, mock_wait, mock_runner_cls, mock_prov_cls,
        tmp_path,
    ):
        from comfy_runner.testing.runpod import run_on_runpod

        mock_prov = mock_prov_cls.return_value
        mock_pod = MagicMock(id="pod123", gpu_type="L40S", datacenter="US", cost_per_hr=0.5, image="img")
        mock_prov.create_pod.return_value = mock_pod
        mock_prov.get_pod_tailscale_url.return_value = None

        mock_runner = mock_runner_cls.return_value
        mock_runner.deploy.return_value = {"ok": True, "job_id": "deploy-job"}
        mock_runner.poll_job.return_value = {"deployed": True}

        mock_suite = MagicMock(name="Test Suite")
        mock_load.return_value = mock_suite

        mock_report = MagicMock(total=2, passed=2, failed=0, duration=3.0)
        mock_build.return_value = mock_report

        config = RunPodTestConfig(
            suite_path=str(tmp_path / "suite"),
            gpu_type="L40S",
            pr=99,
            terminate=True,
        )
        result = run_on_runpod(config)

        assert result.pod_id == "pod123"
        assert result.error is None
        assert result.deploy_result == {"deployed": True}
        assert result.test_result["total"] == 2
        assert result.terminated is True
        mock_prov.terminate_pod.assert_called_once_with("pod123")

    @patch("comfy_runner.testing.runpod.RunPodProvider")
    @patch("comfy_runner.testing.runpod.RemoteRunner")
    @patch("comfy_runner.testing.runpod._wait_for_server")
    @patch("comfy_runner.testing.runpod.get_pod_record", return_value={"id": "existing123"})
    @patch("comfy_runner.testing.runpod.set_pod_record")
    @patch("comfy_runner.testing.runpod.run_suite")
    @patch("comfy_runner.testing.runpod.build_report")
    @patch("comfy_runner.testing.runpod.write_report", return_value={})
    @patch("comfy_runner.testing.runpod.load_suite")
    def test_reuse_existing_pod(
        self, mock_load, mock_write, mock_build, mock_run,
        mock_set_rec, mock_get_rec, mock_wait, mock_runner_cls, mock_prov_cls,
        tmp_path,
    ):
        from comfy_runner.testing.runpod import run_on_runpod

        mock_prov = mock_prov_cls.return_value
        mock_prov.get_pod.return_value = MagicMock(status="RUNNING")
        mock_prov.get_pod_tailscale_url.return_value = None

        mock_runner = mock_runner_cls.return_value
        mock_runner.get_status.return_value = {"running": True}

        mock_load.return_value = MagicMock(name="Suite")
        mock_build.return_value = MagicMock(total=1, passed=1, failed=0, duration=1.0)

        config = RunPodTestConfig(
            suite_path=str(tmp_path / "suite"),
            pod_name="my-pod",
            terminate=True,
        )
        result = run_on_runpod(config)

        assert result.pod_id == "existing123"
        assert result.error is None
        # Should NOT terminate — pod was not created by us
        assert result.terminated is False
        mock_prov.create_pod.assert_not_called()

    @patch("comfy_runner.testing.runpod.RunPodProvider")
    @patch("comfy_runner.testing.runpod._wait_for_server")
    @patch("comfy_runner.testing.runpod.get_pod_record", return_value=None)
    @patch("comfy_runner.testing.runpod.set_pod_record")
    def test_error_still_terminates(
        self, mock_set_rec, mock_get_rec, mock_wait, mock_prov_cls,
    ):
        from comfy_runner.testing.runpod import run_on_runpod

        mock_prov = mock_prov_cls.return_value
        mock_pod = MagicMock(id="pod456", gpu_type="L40S", datacenter="US", cost_per_hr=0.5, image="img")
        mock_prov.create_pod.return_value = mock_pod
        mock_prov.get_pod_tailscale_url.return_value = None

        mock_wait.side_effect = RuntimeError("Server never came up")

        config = RunPodTestConfig(suite_path="/test/suite", terminate=True)
        result = run_on_runpod(config)

        assert result.error is not None
        assert "Server never came up" in result.error
        assert result.terminated is True
        mock_prov.terminate_pod.assert_called_once_with("pod456")

    @patch("comfy_runner.testing.runpod.RunPodProvider")
    @patch("comfy_runner.testing.runpod.RemoteRunner")
    @patch("comfy_runner.testing.runpod._wait_for_server")
    @patch("comfy_runner.testing.runpod.get_pod_record", return_value=None)
    @patch("comfy_runner.testing.runpod.set_pod_record")
    @patch("comfy_runner.testing.runpod.run_suite")
    @patch("comfy_runner.testing.runpod.build_report")
    @patch("comfy_runner.testing.runpod.write_report", return_value={})
    @patch("comfy_runner.testing.runpod.load_suite")
    def test_no_terminate_flag(
        self, mock_load, mock_write, mock_build, mock_run,
        mock_set_rec, mock_get_rec, mock_wait, mock_runner_cls, mock_prov_cls,
        tmp_path,
    ):
        from comfy_runner.testing.runpod import run_on_runpod

        mock_prov = mock_prov_cls.return_value
        mock_pod = MagicMock(id="pod789", gpu_type="L40S", datacenter="US", cost_per_hr=0.5, image="img")
        mock_prov.create_pod.return_value = mock_pod
        mock_prov.get_pod_tailscale_url.return_value = None

        mock_runner = mock_runner_cls.return_value
        mock_runner.get_status.return_value = {"running": True}

        mock_load.return_value = MagicMock(name="Suite")
        mock_build.return_value = MagicMock(total=1, passed=1, failed=0, duration=1.0)

        config = RunPodTestConfig(suite_path=str(tmp_path / "suite"), terminate=False)
        result = run_on_runpod(config)

        assert result.terminated is False
        mock_prov.terminate_pod.assert_not_called()

    @patch("comfy_runner.testing.runpod.RunPodProvider")
    @patch("comfy_runner.testing.runpod._wait_for_server")
    @patch("comfy_runner.testing.runpod.get_pod_record", return_value=None)
    @patch("comfy_runner.testing.runpod.set_pod_record")
    def test_keyboard_interrupt_terminates(
        self, mock_set_rec, mock_get_rec, mock_wait, mock_prov_cls,
    ):
        """Ctrl+C must still terminate the pod."""
        from comfy_runner.testing.runpod import run_on_runpod

        mock_prov = mock_prov_cls.return_value
        mock_pod = MagicMock(id="pod-int", gpu_type="L40S", datacenter="US", cost_per_hr=0.5, image="img")
        mock_prov.create_pod.return_value = mock_pod
        mock_prov.get_pod_tailscale_url.return_value = None

        mock_wait.side_effect = KeyboardInterrupt()

        config = RunPodTestConfig(suite_path="/test/suite", terminate=True)
        result = run_on_runpod(config)

        assert result.error == "Interrupted by user"
        assert result.terminated is True
        mock_prov.terminate_pod.assert_called_once_with("pod-int")

    @patch("comfy_runner.testing.runpod.RunPodProvider")
    @patch("comfy_runner.testing.runpod._wait_for_server")
    @patch("comfy_runner.testing.runpod.get_pod_record", return_value={"id": "dead-pod"})
    @patch("comfy_runner.testing.runpod.set_pod_record")
    def test_terminated_pod_creates_new(
        self, mock_set_rec, mock_get_rec, mock_wait, mock_prov_cls,
    ):
        """If the reused pod is terminated, create a new one."""
        from comfy_runner.testing.runpod import run_on_runpod

        mock_prov = mock_prov_cls.return_value
        # Existing pod is terminated
        mock_prov.get_pod.return_value = MagicMock(status="TERMINATED")
        mock_prov.get_pod_tailscale_url.return_value = None
        # New pod will be created
        mock_new_pod = MagicMock(id="new-pod", gpu_type="L40S", datacenter="US", cost_per_hr=0.5, image="img")
        mock_prov.create_pod.return_value = mock_new_pod

        mock_wait.side_effect = RuntimeError("stop here")

        config = RunPodTestConfig(suite_path="/test/suite", pod_name="old", terminate=True)
        result = run_on_runpod(config)

        # Should have created a new pod
        mock_prov.create_pod.assert_called_once()
        assert result.pod_id == "new-pod"
        # New pod should be terminated on error
        assert result.terminated is True


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestCLIRunpodArgs:
    def test_runpod_flag_parsed(self):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "run", "/fake/suite", "--runpod"])

    def test_target_required_without_runpod(self, capsys):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "run", "/fake/suite"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "--target" in out["error"]

    def test_target_and_runpod_conflict(self, capsys):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "run", "/fake/suite",
                  "--runpod", "--target", "http://localhost:8188"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "--target" in out["error"] and "--runpod" in out["error"]

    def test_pr_branch_commit_exclusive(self):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "run", "/fake/suite",
                  "--runpod", "--pr", "1", "--branch", "main"])
