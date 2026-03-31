"""Tests for comfy_runner.hosted.runpod_provider — pod/volume mapping, defaults, URL logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from comfy_runner.hosted.provider import PodInfo, VolumeInfo
from comfy_runner.hosted.runpod_provider import (
    DEFAULT_IMAGE,
    DEFAULT_PORTS,
    RunPodProvider,
    _pod_info,
    _volume_info,
)


# ---------------------------------------------------------------------------
# _pod_info mapping
# ---------------------------------------------------------------------------

class TestPodInfoMapping:
    def test_full_response(self):
        raw = {
            "id": "pod_1",
            "name": "my-pod",
            "desiredStatus": "RUNNING",
            "gpu": {"displayName": "A100", "id": "a100-80gb"},
            "machine": {"dataCenterId": "US-KS-2"},
            "costPerHr": 0.74,
            "image": "runpod/ubuntu:24.04",
        }
        info = _pod_info(raw)
        assert isinstance(info, PodInfo)
        assert info.id == "pod_1"
        assert info.name == "my-pod"
        assert info.status == "RUNNING"
        assert info.gpu_type == "A100"
        assert info.datacenter == "US-KS-2"
        assert info.cost_per_hr == 0.74
        assert info.image == "runpod/ubuntu:24.04"
        assert info.raw is raw

    def test_missing_fields_use_defaults(self):
        info = _pod_info({})
        assert info.id == ""
        assert info.name == ""
        assert info.status == "UNKNOWN"
        assert info.gpu_type == ""
        assert info.datacenter == ""
        assert info.cost_per_hr == 0.0

    def test_gpu_fallback_to_id(self):
        raw = {"gpu": {"id": "rtx4090"}}
        info = _pod_info(raw)
        assert info.gpu_type == "rtx4090"

    def test_null_gpu_field(self):
        raw = {"gpu": None}
        info = _pod_info(raw)
        assert info.gpu_type == ""

    def test_null_cost(self):
        raw = {"costPerHr": None}
        info = _pod_info(raw)
        assert info.cost_per_hr == 0.0


# ---------------------------------------------------------------------------
# _volume_info mapping
# ---------------------------------------------------------------------------

class TestVolumeInfoMapping:
    def test_full_response(self):
        raw = {"id": "vol_1", "name": "ws", "size": 50, "dataCenterId": "US-KS-2"}
        info = _volume_info(raw)
        assert isinstance(info, VolumeInfo)
        assert info.id == "vol_1"
        assert info.name == "ws"
        assert info.size_gb == 50
        assert info.datacenter == "US-KS-2"
        assert info.raw is raw

    def test_missing_fields(self):
        info = _volume_info({})
        assert info.id == ""
        assert info.size_gb == 0
        assert info.datacenter == ""

    def test_null_size(self):
        info = _volume_info({"size": None})
        assert info.size_gb == 0


# ---------------------------------------------------------------------------
# RunPodProvider — create_pod defaults
# ---------------------------------------------------------------------------

class TestRunPodProviderCreatePod:
    @patch("comfy_runner.hosted.runpod_provider.get_provider_config")
    @patch("comfy_runner.hosted.runpod_provider.get_runpod_api_key")
    def _make_provider(self, mock_key, mock_cfg, cfg_overrides=None):
        mock_key.return_value = "rk_test"
        mock_cfg.return_value = {
            "default_gpu": "NVIDIA L40S",
            "default_datacenter": "US-KS-2",
            "default_cloud_type": "SECURE",
            **(cfg_overrides or {}),
        }
        return RunPodProvider()

    def test_defaults_applied(self):
        prov = self._make_provider()
        prov.api = MagicMock()
        prov.api.create_pod.return_value = {"id": "p1", "name": "test", "desiredStatus": "RUNNING"}

        result = prov.create_pod("test")

        call_kwargs = prov.api.create_pod.call_args[1]
        assert call_kwargs["gpuTypeIds"] == ["NVIDIA L40S"]
        assert call_kwargs["imageName"] == "ghcr.io/kosinkadink/comfy-runner:latest"
        assert call_kwargs["ports"] == DEFAULT_PORTS
        assert call_kwargs["cloudType"] == "SECURE"
        assert call_kwargs["dataCenterIds"] == ["US-KS-2"]
        assert isinstance(result, PodInfo)

    def test_explicit_overrides(self):
        prov = self._make_provider()
        prov.api = MagicMock()
        prov.api.create_pod.return_value = {"id": "p2", "desiredStatus": "RUNNING"}

        prov.create_pod(
            "test2",
            gpu_type="A100",
            image="custom:latest",
            ports=["8080/http"],
            datacenter="EU-RO-1",
            cloud_type="COMMUNITY",
        )

        call_kwargs = prov.api.create_pod.call_args[1]
        assert call_kwargs["gpuTypeIds"] == ["A100"]
        assert call_kwargs["imageName"] == "custom:latest"
        assert call_kwargs["ports"] == ["8080/http"]
        assert call_kwargs["dataCenterIds"] == ["EU-RO-1"]
        assert call_kwargs["cloudType"] == "COMMUNITY"

    def test_volume_id_passed(self):
        prov = self._make_provider()
        prov.api = MagicMock()
        prov.api.create_pod.return_value = {"id": "p3", "desiredStatus": "RUNNING"}

        prov.create_pod("test3", volume_id="vol_abc")

        call_kwargs = prov.api.create_pod.call_args[1]
        assert call_kwargs["networkVolumeId"] == "vol_abc"
        assert "volumeInGb" not in call_kwargs

    def test_volume_size_passed(self):
        prov = self._make_provider()
        prov.api = MagicMock()
        prov.api.create_pod.return_value = {"id": "p4", "desiredStatus": "RUNNING"}

        prov.create_pod("test4", volume_size_gb=100)

        call_kwargs = prov.api.create_pod.call_args[1]
        assert call_kwargs["volumeInGb"] == 100
        assert "networkVolumeId" not in call_kwargs


# ---------------------------------------------------------------------------
# RunPodProvider — get_pod_url
# ---------------------------------------------------------------------------

class TestGetPodUrl:
    @patch("comfy_runner.hosted.runpod_provider.get_provider_config")
    @patch("comfy_runner.hosted.runpod_provider.get_runpod_api_key")
    def test_running_pod_returns_url(self, mock_key, mock_cfg):
        mock_key.return_value = "rk_test"
        mock_cfg.return_value = {}
        prov = RunPodProvider()
        prov.api = MagicMock()
        prov.api.get_pod.return_value = {"id": "pod_x", "desiredStatus": "RUNNING"}

        url = prov.get_pod_url("pod_x", 8188)
        assert url == "https://pod_x-8188.proxy.runpod.net"

    @patch("comfy_runner.hosted.runpod_provider.get_provider_config")
    @patch("comfy_runner.hosted.runpod_provider.get_runpod_api_key")
    def test_exited_pod_returns_none(self, mock_key, mock_cfg):
        mock_key.return_value = "rk_test"
        mock_cfg.return_value = {}
        prov = RunPodProvider()
        prov.api = MagicMock()
        prov.api.get_pod.return_value = {"id": "pod_x", "desiredStatus": "EXITED"}

        assert prov.get_pod_url("pod_x", 8188) is None

    @patch("comfy_runner.hosted.runpod_provider.get_provider_config")
    @patch("comfy_runner.hosted.runpod_provider.get_runpod_api_key")
    def test_missing_pod_returns_none(self, mock_key, mock_cfg):
        mock_key.return_value = "rk_test"
        mock_cfg.return_value = {}
        prov = RunPodProvider()
        prov.api = MagicMock()
        prov.api.get_pod.return_value = None

        assert prov.get_pod_url("pod_x", 8188) is None


# ---------------------------------------------------------------------------
# RunPodProvider — init without API key raises
# ---------------------------------------------------------------------------

class TestProviderInit:
    @patch("comfy_runner.hosted.runpod_provider.get_runpod_api_key")
    def test_no_api_key_raises(self, mock_key):
        mock_key.return_value = ""
        with pytest.raises(RuntimeError, match="API key not set"):
            RunPodProvider()
