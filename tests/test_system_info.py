"""Tests for comfy_runner.system_info — GPU detection, CPU, memory, disk, OS."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from comfy_runner.system_info import (
    _get_cpu_info,
    _get_disk_info,
    _get_linux_gpus_lspci,
    _get_nvidia_driver_version_from_banner,
    _get_nvidia_gpus,
    _get_os_info,
    _get_total_memory_gb,
    _is_nvidia_driver_supported,
    get_system_info,
)


# ---------------------------------------------------------------------------
# Helper: fake CompletedProcess for _run_silent mocks
# ---------------------------------------------------------------------------

def _fake_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode,
        stdout=stdout.encode("utf-8"), stderr=b"",
    )


# ---------------------------------------------------------------------------
# _get_nvidia_driver_version
# ---------------------------------------------------------------------------

class TestGetNvidiaDriverVersionFromBanner:
    def test_parses_banner_output(self, monkeypatch):
        banner = (
            "+-----+\n"
            "| NVIDIA-SMI 580.126.09    Driver Version: 580.126.09 |\n"
            "+-----+\n"
        )
        monkeypatch.setattr(
            "comfy_runner.system_info._run_silent",
            lambda *a, **kw: _fake_result(banner),
        )
        assert _get_nvidia_driver_version_from_banner() == "580.126.09"

    def test_returns_none_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.system_info._run_silent",
            lambda *a, **kw: None,
        )
        assert _get_nvidia_driver_version_from_banner() is None


# ---------------------------------------------------------------------------
# _is_nvidia_driver_supported
# ---------------------------------------------------------------------------

class TestIsNvidiaDriverSupported:
    def test_none_returns_none(self):
        assert _is_nvidia_driver_supported(None) is None

    def test_supported_high(self):
        assert _is_nvidia_driver_supported("591.59") is True

    def test_unsupported_low(self):
        assert _is_nvidia_driver_supported("570.0") is False

    def test_boundary_supported(self):
        assert _is_nvidia_driver_supported("580") is True


# ---------------------------------------------------------------------------
# _get_nvidia_gpus
# ---------------------------------------------------------------------------

class TestDriverVersionExtraction:
    """Driver version is extracted from _get_nvidia_gpus results, not a separate call."""

    def test_extracted_from_gpus(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "nvidia")
        monkeypatch.setattr(
            "comfy_runner.system_info._get_gpus",
            lambda: [{"vendor": "NVIDIA", "model": "RTX 4090", "vram_mb": 24564, "driver_version": "590.10"}],
        )
        info = get_system_info()
        assert info["nvidia_driver_version"] == "590.10"

    def test_falls_back_to_banner_when_gpus_empty(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "nvidia")
        monkeypatch.setattr("comfy_runner.system_info._get_gpus", lambda: [])
        monkeypatch.setattr(
            "comfy_runner.system_info._get_nvidia_driver_version_from_banner",
            lambda: "580.00",
        )
        info = get_system_info()
        assert info["nvidia_driver_version"] == "580.00"

    def test_none_when_no_nvidia(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "amd")
        monkeypatch.setattr("comfy_runner.system_info._get_gpus", lambda: [])
        info = get_system_info()
        assert info["nvidia_driver_version"] is None


class TestGetNvidiaGpus:
    def test_parses_multi_line_csv(self, monkeypatch):
        csv_output = (
            "NVIDIA GeForce RTX 4090, 24564, 560.94\n"
            "NVIDIA GeForce RTX 3090, 24576, 560.94\n"
        )
        monkeypatch.setattr(
            "comfy_runner.system_info._run_silent",
            lambda *a, **kw: _fake_result(csv_output),
        )
        gpus = _get_nvidia_gpus()
        assert len(gpus) == 2
        assert gpus[0]["model"] == "NVIDIA GeForce RTX 4090"
        assert gpus[0]["vram_mb"] == 24564
        assert gpus[0]["driver_version"] == "560.94"
        assert gpus[1]["model"] == "NVIDIA GeForce RTX 3090"
        assert gpus[1]["vram_mb"] == 24576

    def test_returns_empty_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.system_info._run_silent",
            lambda *a, **kw: None,
        )
        assert _get_nvidia_gpus() == []


# ---------------------------------------------------------------------------
# _get_cpu_info
# ---------------------------------------------------------------------------

class TestGetCpuInfo:
    PROC_CPUINFO = (
        "processor\t: 0\n"
        "vendor_id\t: GenuineIntel\n"
        "model name\t: Intel(R) Core(TM) i9-13900K\n"
        "physical id\t: 0\n"
        "core id\t\t: 0\n"
        "cpu MHz\t\t: 3000.000\n"
        "\n"
        "processor\t: 1\n"
        "vendor_id\t: GenuineIntel\n"
        "model name\t: Intel(R) Core(TM) i9-13900K\n"
        "physical id\t: 0\n"
        "core id\t\t: 1\n"
        "cpu MHz\t\t: 3000.000\n"
    )

    def test_linux_parsing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "linux")
        cpuinfo_file = tmp_path / "cpuinfo"
        cpuinfo_file.write_text(self.PROC_CPUINFO)
        monkeypatch.setattr(
            "comfy_runner.system_info._PROC_CPUINFO",
            str(cpuinfo_file),
        )
        info = _get_cpu_info()
        assert info["model"] == "Intel(R) Core(TM) i9-13900K"
        assert info["physical_cores"] == 2
        assert info["manufacturer"] == "Intel"
        assert info["speed_ghz"] == 3.0


# ---------------------------------------------------------------------------
# _get_os_info
# ---------------------------------------------------------------------------

class TestGetOsInfo:
    def test_returns_required_fields(self):
        info = _get_os_info()
        assert "platform" in info
        assert "arch" in info
        assert "os_version" in info
        assert "os_distro" in info
        assert "os_release" in info


# ---------------------------------------------------------------------------
# _get_total_memory_gb
# ---------------------------------------------------------------------------

class TestGetTotalMemoryGb:
    def test_linux_meminfo_parsing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.platform", "linux")
        meminfo_file = tmp_path / "meminfo"
        meminfo_file.write_text(
            "MemTotal:       16384000 kB\n"
            "MemFree:         8192000 kB\n"
            "MemAvailable:   12000000 kB\n"
        )
        monkeypatch.setattr(
            "comfy_runner.system_info._PROC_MEMINFO",
            str(meminfo_file),
        )
        result = _get_total_memory_gb()
        assert result == 16


# ---------------------------------------------------------------------------
# _get_disk_info
# ---------------------------------------------------------------------------

class TestGetDiskInfo:
    def test_with_tmp_path(self, tmp_path):
        info = _get_disk_info(str(tmp_path))
        assert isinstance(info["free_gb"], float)
        assert isinstance(info["total_gb"], float)
        assert info["free_gb"] > 0
        assert info["total_gb"] > 0


# ---------------------------------------------------------------------------
# get_system_info — full shape validation
# ---------------------------------------------------------------------------

class TestGetSystemInfo:
    def test_full_shape(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "nvidia")
        monkeypatch.setattr(
            "comfy_runner.system_info._get_nvidia_gpus",
            lambda: [{"vendor": "NVIDIA", "model": "RTX 4090", "vram_mb": 24564, "driver_version": "590.10"}],
        )
        monkeypatch.setattr("comfy_runner.system_info._get_linux_gpus_lspci", lambda: [])

        info = get_system_info()

        expected_keys = {
            "gpu_vendor", "gpu_label", "gpus",
            "nvidia_driver_version", "nvidia_driver_supported",
            "platform", "arch", "os_version", "os_distro", "os_release",
            "total_memory_gb",
            "cpu_model", "cpu_cores", "cpu_physical_cores",
            "cpu_speed_ghz", "cpu_manufacturer",
            "disk_free_gb", "disk_total_gb",
            "installation_count", "installations",
        }
        assert expected_keys.issubset(info.keys())
        assert info["gpu_vendor"] == "nvidia"
        assert info["gpu_label"] == "NVIDIA"
        assert len(info["gpus"]) >= 1
        # Driver version extracted from GPU data, not a separate nvidia-smi call
        assert info["nvidia_driver_version"] == "590.10"
        assert info["nvidia_driver_supported"] is True


# ---------------------------------------------------------------------------
# GPU label mapping
# ---------------------------------------------------------------------------

class TestGpuLabelMapping:
    def test_cpu_maps_to_none(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "cpu")
        monkeypatch.setattr("comfy_runner.system_info._get_gpus", lambda: [])

        info = get_system_info()
        assert info["gpu_vendor"] is None
        assert info["gpu_label"] is None

    def test_nvidia_maps_to_label(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "nvidia")
        monkeypatch.setattr(
            "comfy_runner.system_info._get_gpus",
            lambda: [{"vendor": "NVIDIA", "model": "RTX 4090", "vram_mb": 24564, "driver_version": "590.10"}],
        )

        info = get_system_info()
        assert info["gpu_label"] == "NVIDIA"

    def test_amd_maps_to_label(self, monkeypatch, tmp_config_dir):
        monkeypatch.setattr("comfy_runner.system_info.detect_gpu", lambda: "amd")
        monkeypatch.setattr("comfy_runner.system_info._get_gpus", lambda: [])

        info = get_system_info()
        assert info["gpu_vendor"] == "amd"
        assert info["gpu_label"] == "AMD"


# ---------------------------------------------------------------------------
# _get_linux_gpus_lspci
# ---------------------------------------------------------------------------

class TestGetLinuxGpusLspci:
    LSPCI_OUTPUT = (
        "Slot:\t01:00.0\n"
        "Class:\tVGA compatible controller\n"
        "Vendor:\tNVIDIA Corporation\n"
        "Device:\tGA102 [GeForce RTX 3090]\n"
        "SVendor:\tASUS\n"
        "SDevice:\tGA102 [GeForce RTX 3090]\n"
        "Rev:\ta1\n"
        "\n"
        "Slot:\t02:00.0\n"
        "Class:\tVGA compatible controller\n"
        "Vendor:\tAdvanced Micro Devices, Inc. [AMD/ATI]\n"
        "Device:\tNavi 21 [Radeon RX 6800]\n"
        "SVendor:\tSapphire\n"
        "SDevice:\tNitro+ RX 6800\n"
        "Rev:\tc1\n"
    )

    def test_parses_lspci_vmm_output(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.system_info._run_silent",
            lambda *a, **kw: _fake_result(self.LSPCI_OUTPUT),
        )
        gpus = _get_linux_gpus_lspci()
        assert len(gpus) == 2
        assert gpus[0]["vendor"] == "NVIDIA"
        assert "RTX 3090" in gpus[0]["model"]
        assert gpus[1]["vendor"] == "AMD"
        assert "Radeon RX 6800" in gpus[1]["model"]
