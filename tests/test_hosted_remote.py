"""Tests for RemoteRunner client and hosted deploy/status/start/stop/logs CLI."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from comfy_runner.hosted.remote import RemoteRunner
from comfy_runner_cli.cli import main


# ---------------------------------------------------------------------------
# RemoteRunner._request
# ---------------------------------------------------------------------------

class TestRemoteRunnerRequest:
    def _make_runner(self):
        return RemoteRunner("https://pod-9189.proxy.runpod.net")

    @patch("comfy_runner.hosted.remote.requests.request")
    def test_success(self, mock_req):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "running": True}
        mock_req.return_value = resp
        runner = self._make_runner()
        data = runner._request("GET", "/main/status")
        assert data["running"] is True

    @patch("comfy_runner.hosted.remote.requests.request")
    def test_connection_error(self, mock_req):
        mock_req.side_effect = requests.ConnectionError("refused")
        runner = self._make_runner()
        with pytest.raises(RuntimeError, match="Failed to connect"):
            runner._request("GET", "/status")

    @patch("comfy_runner.hosted.remote.requests.request")
    def test_non_ok_response(self, mock_req):
        resp = MagicMock()
        resp.ok = False
        resp.json.return_value = {"ok": False, "error": "Not found"}
        mock_req.return_value = resp
        runner = self._make_runner()
        with pytest.raises(RuntimeError, match="Not found"):
            runner._request("GET", "/missing/status")

    @patch("comfy_runner.hosted.remote.requests.request")
    def test_invalid_json(self, mock_req):
        resp = MagicMock()
        resp.ok = True
        resp.json.side_effect = requests.JSONDecodeError("err", "doc", 0)
        resp.text = "<html>"
        mock_req.return_value = resp
        runner = self._make_runner()
        with pytest.raises(RuntimeError, match="invalid JSON"):
            runner._request("GET", "/status")


# ---------------------------------------------------------------------------
# RemoteRunner.poll_job
# ---------------------------------------------------------------------------

class TestPollJob:
    @patch("comfy_runner.hosted.remote.time.sleep")
    @patch("comfy_runner.hosted.remote.requests.request")
    def test_poll_done(self, mock_req, mock_sleep):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "status": "done", "result": {"port": 8188}}
        mock_req.return_value = resp
        runner = RemoteRunner("http://localhost:9189")
        result = runner.poll_job("abc123")
        assert result == {"port": 8188}

    @patch("comfy_runner.hosted.remote.time.sleep")
    @patch("comfy_runner.hosted.remote.requests.request")
    def test_poll_error(self, mock_req, mock_sleep):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "status": "error", "error": "init failed"}
        mock_req.return_value = resp
        runner = RemoteRunner("http://localhost:9189")
        with pytest.raises(RuntimeError, match="init failed"):
            runner.poll_job("abc123")

    @patch("comfy_runner.hosted.remote.time.sleep")
    @patch("comfy_runner.hosted.remote.requests.request")
    def test_poll_cancelled(self, mock_req, mock_sleep):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "status": "cancelled"}
        mock_req.return_value = resp
        runner = RemoteRunner("http://localhost:9189")
        with pytest.raises(RuntimeError, match="cancelled"):
            runner.poll_job("abc123")

    @patch("comfy_runner.hosted.remote.time.monotonic")
    @patch("comfy_runner.hosted.remote.time.sleep")
    @patch("comfy_runner.hosted.remote.requests.request")
    def test_poll_timeout(self, mock_req, mock_sleep, mock_time):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "status": "running"}
        mock_req.return_value = resp
        # First call sets deadline, second call exceeds it
        mock_time.side_effect = [0, 999]
        runner = RemoteRunner("http://localhost:9189")
        with pytest.raises(RuntimeError, match="timed out"):
            runner.poll_job("abc123", timeout=10)


# ---------------------------------------------------------------------------
# RemoteRunner high-level methods
# ---------------------------------------------------------------------------

class TestRemoteRunnerMethods:
    @patch("comfy_runner.hosted.remote.requests.request")
    def test_deploy(self, mock_req):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "job_id": "j1", "async": True}
        mock_req.return_value = resp
        runner = RemoteRunner("http://localhost:9189")
        data = runner.deploy("main", pr=1234, start=True)
        assert data["job_id"] == "j1"
        call_kwargs = mock_req.call_args
        body = call_kwargs[1]["json"]
        assert body["pr"] == 1234
        assert body["start"] is True

    @patch("comfy_runner.hosted.remote.requests.request")
    def test_stop(self, mock_req):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"ok": True, "was_running": True}
        mock_req.return_value = resp
        runner = RemoteRunner("http://localhost:9189")
        data = runner.stop("main")
        assert data["was_running"] is True


# ---------------------------------------------------------------------------
# CLI: hosted deploy / status / start-comfy / stop-comfy / logs
# ---------------------------------------------------------------------------

def _setup_pod_record(tmp_config_dir):
    from comfy_runner.hosted.config import set_pod_record, set_provider_value
    # Seed RunPod + Tailscale config so _resolve_server_url can produce
    # a URL (pods are Tailscale-only now -- no public proxy fallback).
    # RunPodProvider() also requires an api_key during __init__.
    set_provider_value("runpod", "api_key", "rk-test")
    set_provider_value("runpod", "tailscale_auth_key", "tskey-auth-test")
    set_provider_value("runpod", "tailscale_domain", "example.ts.net")
    set_pod_record("runpod", "my-pod", {"id": "pod_abc"})


class TestHostedDeployCLI:
    def test_deploy_requires_pod_name(self):
        with pytest.raises(SystemExit):
            main(["hosted", "deploy"])

    def test_deploy_runs(self, tmp_config_dir, capsys):
        _setup_pod_record(tmp_config_dir)
        with patch("comfy_runner.hosted.remote.requests.request") as mock_req:
            # deploy returns async
            deploy_resp = MagicMock()
            deploy_resp.ok = True
            deploy_resp.json.return_value = {"ok": True, "job_id": "j1", "async": True}
            # poll returns done
            poll_resp = MagicMock()
            poll_resp.ok = True
            poll_resp.json.return_value = {"ok": True, "status": "done", "result": {"port": 8188}}
            mock_req.side_effect = [deploy_resp, poll_resp]
            main(["--json", "hosted", "deploy", "my-pod", "--pr", "1234"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["job_id"] == "j1"


class TestHostedStatusCLI:
    def test_status_requires_pod_name(self):
        with pytest.raises(SystemExit):
            main(["hosted", "status"])

    def test_status_runs(self, tmp_config_dir, capsys):
        _setup_pod_record(tmp_config_dir)
        with patch("comfy_runner.hosted.remote.requests.request") as mock_req:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {"ok": True, "installations": []}
            mock_req.return_value = resp
            main(["--json", "hosted", "status", "my-pod"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True


class TestHostedStartStopCLI:
    def test_start_comfy(self, tmp_config_dir, capsys):
        _setup_pod_record(tmp_config_dir)
        with patch("comfy_runner.hosted.remote.requests.request") as mock_req:
            restart_resp = MagicMock()
            restart_resp.ok = True
            restart_resp.json.return_value = {"ok": True, "job_id": "j2", "async": True}
            poll_resp = MagicMock()
            poll_resp.ok = True
            poll_resp.json.return_value = {"ok": True, "status": "done", "result": {"port": 8188}}
            mock_req.side_effect = [restart_resp, poll_resp]
            main(["--json", "hosted", "start-comfy", "my-pod"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True

    def test_stop_comfy(self, tmp_config_dir, capsys):
        _setup_pod_record(tmp_config_dir)
        with patch("comfy_runner.hosted.remote.requests.request") as mock_req:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {"ok": True, "was_running": True}
            mock_req.return_value = resp
            main(["--json", "hosted", "stop-comfy", "my-pod"])
        out = json.loads(capsys.readouterr().out)
        assert out["was_running"] is True


class TestHostedLogsCLI:
    def test_logs(self, tmp_config_dir, capsys):
        _setup_pod_record(tmp_config_dir)
        with patch("comfy_runner.hosted.remote.requests.request") as mock_req:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {"ok": True, "lines": ["line1\n", "line2\n"]}
            mock_req.return_value = resp
            main(["--json", "hosted", "logs", "my-pod"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True


class TestResolveServerUrl:
    def test_missing_pod_raises(self, tmp_config_dir):
        from comfy_runner_cli.cli import _resolve_server_url
        with pytest.raises(RuntimeError, match="No pod record"):
            _resolve_server_url("nonexistent")

    def test_resolves_correctly(self, tmp_config_dir):
        from comfy_runner.hosted.config import set_pod_record, set_provider_value
        from comfy_runner_cli.cli import _resolve_server_url
        # Tailscale is now the only path -- seed config so resolution works.
        set_provider_value("runpod", "api_key", "rk-test")
        set_provider_value("runpod", "tailscale_auth_key", "tskey-auth-test")
        set_provider_value("runpod", "tailscale_domain", "example.ts.net")
        set_pod_record("runpod", "test", {"id": "pod_xyz"})
        assert _resolve_server_url("test") == "http://comfy-test.example.ts.net:9189"

    def test_no_tailscale_raises(self, tmp_config_dir):
        from comfy_runner.hosted.config import set_pod_record, set_provider_value
        from comfy_runner_cli.cli import _resolve_server_url
        # Pod record exists, RunPod API key is set, but Tailscale isn't
        # configured -- should raise.
        set_provider_value("runpod", "api_key", "rk-test")
        set_pod_record("runpod", "test", {"id": "pod_xyz"})
        with pytest.raises(RuntimeError, match="Tailscale is not configured"):
            _resolve_server_url("test")
