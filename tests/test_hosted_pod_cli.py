"""Tests for CLI hosted pod commands — create, list, show, start, stop, terminate, url."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from comfy_runner.hosted.provider import PodInfo
from comfy_runner_cli.cli import main


class _MockProviderCtx:
    """Patch RunPodProvider with sensible defaults for URL lookups.

    Pods are Tailscale-only now; tests that don't care about URLs get
    a deterministic ``None`` from ``get_pod_tailscale_url`` instead of
    a non-serializable ``MagicMock``.
    """
    def __enter__(self):
        self._patch = patch("comfy_runner.hosted.runpod_provider.RunPodProvider")
        MockProv = self._patch.__enter__()
        MockProv.return_value.get_pod_tailscale_url.return_value = None
        return MockProv
    def __exit__(self, *a):
        return self._patch.__exit__(*a)


def _mock_provider():
    """Return a patched RunPodProvider context manager."""
    return _MockProviderCtx()


def _make_pod(**overrides) -> PodInfo:
    defaults = dict(
        id="pod_abc", name="test-pod", status="RUNNING",
        gpu_type="NVIDIA L40S", datacenter="US-KS-2",
        cost_per_hr=0.74, image="runpod/ubuntu:24.04", raw={},
    )
    defaults.update(overrides)
    return PodInfo(**defaults)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestPodArgParsing:
    def test_create_requires_name(self):
        with pytest.raises(SystemExit):
            main(["hosted", "pod", "create"])

    def test_show_requires_pod_id(self):
        with pytest.raises(SystemExit):
            main(["hosted", "pod", "show"])

    def test_start_requires_pod_id(self):
        with pytest.raises(SystemExit):
            main(["hosted", "pod", "start"])

    def test_stop_requires_pod_id(self):
        with pytest.raises(SystemExit):
            main(["hosted", "pod", "stop"])

    def test_terminate_requires_pod_id(self):
        with pytest.raises(SystemExit):
            main(["hosted", "pod", "terminate"])

    def test_url_requires_pod_id(self):
        with pytest.raises(SystemExit):
            main(["hosted", "pod", "url"])


# ---------------------------------------------------------------------------
# pod create
# ---------------------------------------------------------------------------

class TestPodCreate:
    def test_create_with_defaults(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "pod", "create", "--name", "test-pod"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["pod"]["id"] == "pod_abc"
        assert out["pod"]["name"] == "test-pod"

    def test_create_with_all_options(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            main([
                "--json", "hosted", "pod", "create",
                "--name", "my-pod",
                "--gpu", "A100",
                "--image", "custom:latest",
                "--region", "EU-RO-1",
                "--cloud-type", "COMMUNITY",
            ])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        # Verify the provider was called with the right args
        call_kwargs = MockProv.return_value.create_pod.call_args[1]
        assert call_kwargs["name"] == "my-pod"
        assert call_kwargs["gpu_type"] == "A100"
        assert call_kwargs["image"] == "custom:latest"
        assert call_kwargs["datacenter"] == "EU-RO-1"
        assert call_kwargs["cloud_type"] == "COMMUNITY"

    def test_create_with_volume_name_lookup(self, capsys):
        with _mock_provider() as MockProv, \
             patch("comfy_runner.hosted.config.get_volume_config") as mock_vol:
            mock_vol.return_value = {"id": "vol_resolved"}
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "pod", "create", "--name", "p", "--volume", "workspace"])
        call_kwargs = MockProv.return_value.create_pod.call_args[1]
        assert call_kwargs["volume_id"] == "vol_resolved"

    def test_create_with_raw_volume_id(self, capsys):
        with _mock_provider() as MockProv, \
             patch("comfy_runner.hosted.config.get_volume_config") as mock_vol:
            mock_vol.return_value = None  # not found in config
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "pod", "create", "--name", "p", "--volume", "vol_raw123"])
        call_kwargs = MockProv.return_value.create_pod.call_args[1]
        assert call_kwargs["volume_id"] == "vol_raw123"

    def test_create_error(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.side_effect = RuntimeError("GPU unavailable")
            with pytest.raises(SystemExit):
                main(["--json", "hosted", "pod", "create", "--name", "fail"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "GPU unavailable" in out["error"]


# ---------------------------------------------------------------------------
# pod list
# ---------------------------------------------------------------------------

class TestPodList:
    def test_list_empty(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.list_pods.return_value = []
            main(["--json", "hosted", "pod", "list"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["pods"] == []

    def test_list_with_pods(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.list_pods.return_value = [
                _make_pod(id="p1", name="pod-1"),
                _make_pod(id="p2", name="pod-2", status="EXITED"),
            ]
            main(["--json", "hosted", "pod", "list"])
        out = json.loads(capsys.readouterr().out)
        assert len(out["pods"]) == 2
        assert out["pods"][0]["id"] == "p1"
        assert out["pods"][1]["status"] == "EXITED"


# ---------------------------------------------------------------------------
# pod show
# ---------------------------------------------------------------------------

class TestPodShow:
    def test_show_found(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.get_pod.return_value = _make_pod()
            main(["--json", "hosted", "pod", "show", "pod_abc"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["pod"]["gpu_type"] == "NVIDIA L40S"

    def test_show_not_found(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.get_pod.return_value = None
            with pytest.raises(SystemExit):
                main(["--json", "hosted", "pod", "show", "pod_missing"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False


# ---------------------------------------------------------------------------
# pod start / stop / terminate
# ---------------------------------------------------------------------------

class TestPodLifecycle:
    def test_start(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.start_pod.return_value = _make_pod(status="RUNNING")
            main(["--json", "hosted", "pod", "start", "pod_abc"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["pod"]["status"] == "RUNNING"

    def test_stop(self, capsys):
        with _mock_provider() as MockProv:
            main(["--json", "hosted", "pod", "stop", "pod_abc"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        MockProv.return_value.stop_pod.assert_called_once_with("pod_abc")

    def test_terminate(self, capsys):
        with _mock_provider() as MockProv:
            main(["--json", "hosted", "pod", "terminate", "pod_abc"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        MockProv.return_value.terminate_pod.assert_called_once_with("pod_abc")


# ---------------------------------------------------------------------------
# pod url
# ---------------------------------------------------------------------------

class TestPodUrl:
    def test_url_running(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.get_pod_url.return_value = "https://pod_abc-8188.proxy.runpod.net"
            main(["--json", "hosted", "pod", "url", "pod_abc"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["url"] == "https://pod_abc-8188.proxy.runpod.net"

    def test_url_not_running(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.get_pod_url.return_value = None
            main(["--json", "hosted", "pod", "url", "pod_abc"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["url"] is None

    def test_url_custom_port(self, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.get_pod_url.return_value = "https://pod_abc-9189.proxy.runpod.net"
            main(["--json", "hosted", "pod", "url", "pod_abc", "--port", "9189"])
        MockProv.return_value.get_pod_url.assert_called_once_with("pod_abc", 9189)
