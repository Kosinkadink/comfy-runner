"""Tests for hosted pod records and hosted init CLI command."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from comfy_runner.hosted.config import (
    get_pod_record,
    list_pod_records,
    remove_pod_record,
    set_pod_record,
    set_provider_value,
)
from comfy_runner.hosted.provider import PodInfo, VolumeInfo
from comfy_runner_cli.cli import main


# ---------------------------------------------------------------------------
# Pod record CRUD
# ---------------------------------------------------------------------------

class TestPodRecordCRUD:
    def test_get_empty(self, tmp_config_dir):
        assert get_pod_record("runpod", "my-pod") is None

    def test_set_and_get(self, tmp_config_dir):
        set_pod_record("runpod", "my-pod", {"id": "pod_1", "gpu_type": "A100"})
        rec = get_pod_record("runpod", "my-pod")
        assert rec is not None
        assert rec["id"] == "pod_1"

    def test_list_empty(self, tmp_config_dir):
        assert list_pod_records("runpod") == {}

    def test_list_multiple(self, tmp_config_dir):
        set_pod_record("runpod", "p1", {"id": "a"})
        set_pod_record("runpod", "p2", {"id": "b"})
        assert set(list_pod_records("runpod").keys()) == {"p1", "p2"}

    def test_remove_existing(self, tmp_config_dir):
        set_pod_record("runpod", "rm_me", {"id": "x"})
        assert remove_pod_record("runpod", "rm_me") is True
        assert get_pod_record("runpod", "rm_me") is None

    def test_remove_missing(self, tmp_config_dir):
        assert remove_pod_record("runpod", "nope") is False

    def test_update(self, tmp_config_dir):
        set_pod_record("runpod", "p", {"id": "old"})
        set_pod_record("runpod", "p", {"id": "new"})
        assert get_pod_record("runpod", "p")["id"] == "new"

    def test_pods_is_reserved_key(self, tmp_config_dir):
        with pytest.raises(ValueError, match="pods"):
            set_provider_value("runpod", "pods", "bad")


# ---------------------------------------------------------------------------
# pod create saves record
# ---------------------------------------------------------------------------

class _MockProviderCtx:
    """Patch RunPodProvider with sensible defaults for URL lookups.

    Pods are Tailscale-only now; ``get_pod_tailscale_url`` returns
    ``None`` by default so JSON output is serializable.
    """
    def __enter__(self):
        self._patch = patch("comfy_runner.hosted.runpod_provider.RunPodProvider")
        MockProv = self._patch.__enter__()
        MockProv.return_value.get_pod_tailscale_url.return_value = None
        return MockProv
    def __exit__(self, *a):
        return self._patch.__exit__(*a)


def _mock_provider():
    return _MockProviderCtx()


def _make_pod(**overrides) -> PodInfo:
    defaults = dict(
        id="pod_abc", name="test-pod", status="RUNNING",
        gpu_type="NVIDIA L40S", datacenter="US-KS-2",
        cost_per_hr=0.74, image="runpod/ubuntu:24.04", raw={},
    )
    defaults.update(overrides)
    return PodInfo(**defaults)


class TestPodCreateSavesRecord:
    def test_create_saves_record(self, tmp_config_dir, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "pod", "create", "--name", "test-pod"])
        rec = get_pod_record("runpod", "test-pod")
        assert rec is not None
        assert rec["id"] == "pod_abc"
        assert rec["gpu_type"] == "NVIDIA L40S"

    def test_create_with_volume_saves_volume_name(self, tmp_config_dir, capsys):
        from comfy_runner.hosted.config import set_volume_config
        set_volume_config("runpod", "workspace", {"id": "vol_123"})
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "pod", "create", "--name", "p", "--volume", "workspace"])
        rec = get_pod_record("runpod", "p")
        assert rec["volume_id"] == "vol_123"
        assert rec["volume_name"] == "workspace"

    def test_create_json_includes_urls(self, tmp_config_dir, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            # Pretend Tailscale is configured so URLs are returned.
            MockProv.return_value.get_pod_tailscale_url.side_effect = (
                lambda name, port=9189: f"http://comfy-{name}.example.ts.net:{port}"
            )
            main(["--json", "hosted", "pod", "create", "--name", "test-pod"])
        out = json.loads(capsys.readouterr().out)
        assert "server_url" in out["pod"]
        assert "comfy_url" in out["pod"]
        # Pods are Tailscale-only; URLs use the MagicDNS hostname.
        assert "comfy-test-pod" in out["pod"]["server_url"]
        assert ":9189" in out["pod"]["server_url"]
        assert ":8188" in out["pod"]["comfy_url"]


class TestPodTerminateRemovesRecord:
    def test_terminate_removes_record(self, tmp_config_dir, capsys):
        set_pod_record("runpod", "my-pod", {"id": "pod_xyz"})
        with _mock_provider() as MockProv:
            main(["--json", "hosted", "pod", "terminate", "pod_xyz"])
        assert get_pod_record("runpod", "my-pod") is None


# ---------------------------------------------------------------------------
# hosted init
# ---------------------------------------------------------------------------

class TestHostedInit:
    def test_init_creates_pod(self, tmp_config_dir, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "init", "--name", "my-comfy"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["pod"]["id"] == "pod_abc"
        rec = get_pod_record("runpod", "my-comfy")
        assert rec is not None

    def test_init_reuses_existing_volume(self, tmp_config_dir, capsys):
        from comfy_runner.hosted.config import set_volume_config
        set_volume_config("runpod", "ws", {"id": "vol_existing"})
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "init", "--name", "p", "--volume", "ws"])
        call_kwargs = MockProv.return_value.create_pod.call_args[1]
        assert call_kwargs["volume_id"] == "vol_existing"
        # Should NOT have called create_volume
        MockProv.return_value.create_volume.assert_not_called()

    def test_init_creates_new_volume(self, tmp_config_dir, capsys):
        mock_vol = MagicMock()
        mock_vol.id = "vol_new"
        mock_vol.datacenter = "US-KS-2"
        mock_vol.size_gb = 100
        with _mock_provider() as MockProv:
            MockProv.return_value.create_volume.return_value = mock_vol
            MockProv.return_value.create_pod.return_value = _make_pod()
            main(["--json", "hosted", "init", "--name", "p",
                  "--volume", "new-vol", "--volume-size", "100"])
        MockProv.return_value.create_volume.assert_called_once()
        from comfy_runner.hosted.config import get_volume_config
        vol = get_volume_config("runpod", "new-vol")
        assert vol is not None
        assert vol["id"] == "vol_new"
        out = json.loads(capsys.readouterr().out)
        assert out["volume"]["id"] == "vol_new"

    def test_init_requires_name(self):
        with pytest.raises(SystemExit):
            main(["hosted", "init"])

    def test_init_error_reports_failure(self, tmp_config_dir, capsys):
        with _mock_provider() as MockProv:
            MockProv.return_value.create_pod.side_effect = RuntimeError("No GPUs available")
            with pytest.raises(SystemExit):
                main(["--json", "hosted", "init", "--name", "fail"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
