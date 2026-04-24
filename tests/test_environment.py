"""Tests for comfy_runner.environment — pure / mockable logic only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestGetUvPath:
    def test_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import get_uv_path
        result = get_uv_path("/opt/install")
        assert result == Path("/opt/install/standalone-env/bin/uv")

    def test_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from comfy_runner.environment import get_uv_path
        result = get_uv_path("/opt/install")
        assert result == Path("/opt/install/standalone-env/uv.exe")


class TestGetMasterPythonPath:
    def test_linux(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import get_master_python_path
        result = get_master_python_path("/opt/install")
        assert result == Path("/opt/install/standalone-env/bin/python3")

    def test_windows(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from comfy_runner.environment import get_master_python_path
        result = get_master_python_path("/opt/install")
        assert result == Path("/opt/install/standalone-env/python.exe")


class TestGetVenvDir:
    def test_returns_correct_path(self):
        from comfy_runner.environment import get_venv_dir
        result = get_venv_dir("/opt/install")
        assert result == Path("/opt/install/ComfyUI/.venv")


# ---------------------------------------------------------------------------
# get_active_venv_dir
# ---------------------------------------------------------------------------

class TestGetActiveVenvDir:
    def test_prefers_venv(self, tmp_path):
        from comfy_runner.environment import get_active_venv_dir
        venv = tmp_path / "ComfyUI" / ".venv"
        venv.mkdir(parents=True)
        result = get_active_venv_dir(tmp_path)
        assert result == venv

    def test_legacy_fallback(self, tmp_path):
        from comfy_runner.environment import get_active_venv_dir
        legacy = tmp_path / "envs" / "default"
        legacy.mkdir(parents=True)
        result = get_active_venv_dir(tmp_path)
        assert result == legacy

    def test_none_when_neither_exists(self, tmp_path):
        from comfy_runner.environment import get_active_venv_dir
        assert get_active_venv_dir(tmp_path) is None


# ---------------------------------------------------------------------------
# get_active_python_path
# ---------------------------------------------------------------------------

class TestGetActivePythonPath:
    def test_returns_python_when_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import get_active_python_path
        venv = tmp_path / "ComfyUI" / ".venv"
        py = venv / "bin" / "python3"
        py.parent.mkdir(parents=True)
        py.touch()
        result = get_active_python_path(tmp_path)
        assert result == py

    def test_returns_none_when_binary_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import get_active_python_path
        venv = tmp_path / "ComfyUI" / ".venv"
        venv.mkdir(parents=True)
        # Directory exists but python3 binary does not
        assert get_active_python_path(tmp_path) is None

    def test_returns_none_when_no_venv(self, tmp_path):
        from comfy_runner.environment import get_active_python_path
        assert get_active_python_path(tmp_path) is None


# ---------------------------------------------------------------------------
# find_site_packages
# ---------------------------------------------------------------------------

class TestFindSitePackages:
    def test_linux_layout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import find_site_packages
        sp = tmp_path / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)
        assert find_site_packages(tmp_path) == sp

    def test_windows_layout(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from comfy_runner.environment import find_site_packages
        sp = tmp_path / "Lib" / "site-packages"
        sp.mkdir(parents=True)
        assert find_site_packages(tmp_path) == sp

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import find_site_packages
        assert find_site_packages(tmp_path) is None


# ---------------------------------------------------------------------------
# _strip_platform
# ---------------------------------------------------------------------------

class TestStripPlatform:
    @pytest.mark.parametrize("input_val,expected", [
        ("win-nvidia", "nvidia"),
        ("mac-mps", "mps"),
        ("linux-amd", "amd"),
        ("cpu", "cpu"),
    ])
    def test_strips(self, input_val, expected):
        from comfy_runner.environment import _strip_platform
        assert _strip_platform(input_val) == expected


# ---------------------------------------------------------------------------
# recommend_variant
# ---------------------------------------------------------------------------

class TestRecommendVariant:
    @pytest.mark.parametrize("variant,gpu,expected", [
        ("linux-nvidia", "nvidia", True),
        ("linux-nvidia-cu126", "nvidia", True),
        ("linux-amd", "nvidia", False),
        ("linux-amd", "amd", True),
        ("mac-mps", "mps", True),
        ("linux-intel-xpu", "intel", True),
        ("linux-intel-xpu-2025", "intel", True),
        ("linux-cpu", "cpu", True),
        ("linux-nvidia", "cpu", False),
        ("linux-cpu", "amd", False),
    ])
    def test_matching(self, variant, gpu, expected):
        from comfy_runner.environment import recommend_variant
        assert recommend_variant(variant, gpu) is expected


# ---------------------------------------------------------------------------
# get_variant_label
# ---------------------------------------------------------------------------

class TestGetVariantLabel:
    @pytest.mark.parametrize("variant,expected", [
        ("linux-nvidia", "NVIDIA"),
        ("win-amd", "AMD"),
        ("mac-mps", "Apple Silicon (MPS)"),
        ("linux-intel-xpu", "Intel Arc (XPU)"),
        ("linux-cpu", "CPU"),
        ("linux-nvidia-cu126", "NVIDIA (CU126)"),
    ])
    def test_labels(self, variant, expected):
        from comfy_runner.environment import get_variant_label
        assert get_variant_label(variant) == expected


# ---------------------------------------------------------------------------
# get_platform_prefix
# ---------------------------------------------------------------------------

class TestGetPlatformPrefix:
    @pytest.mark.parametrize("system,expected", [
        ("Linux", "linux-"),
        ("Windows", "win-"),
        ("Darwin", "mac-"),
    ])
    def test_prefixes(self, monkeypatch, system, expected):
        monkeypatch.setattr("platform.system", lambda: system)
        from comfy_runner.environment import get_platform_prefix
        assert get_platform_prefix() == expected

    def test_unsupported_raises(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "FreeBSD")
        from comfy_runner.environment import get_platform_prefix
        with pytest.raises(RuntimeError, match="Unsupported platform"):
            get_platform_prefix()


# ---------------------------------------------------------------------------
# _is_download_complete
# ---------------------------------------------------------------------------

class TestIsDownloadComplete:
    def test_missing_file(self, tmp_path):
        from comfy_runner.environment import _is_download_complete
        assert _is_download_complete(tmp_path / "missing.tar.gz") is False

    def test_file_with_meta_sidecar(self, tmp_path):
        from comfy_runner.environment import _is_download_complete, DL_META_SUFFIX
        f = tmp_path / "archive.tar.gz"
        f.write_bytes(b"x" * 100)
        meta = Path(str(f) + DL_META_SUFFIX)
        meta.write_text("{}")
        assert _is_download_complete(f) is False

    def test_complete_no_size_check(self, tmp_path):
        from comfy_runner.environment import _is_download_complete
        f = tmp_path / "archive.tar.gz"
        f.write_bytes(b"x" * 100)
        assert _is_download_complete(f) is True

    def test_size_match(self, tmp_path):
        from comfy_runner.environment import _is_download_complete
        f = tmp_path / "archive.tar.gz"
        f.write_bytes(b"x" * 200)
        assert _is_download_complete(f, 200) is True

    def test_size_mismatch(self, tmp_path):
        from comfy_runner.environment import _is_download_complete
        f = tmp_path / "archive.tar.gz"
        f.write_bytes(b"x" * 100)
        assert _is_download_complete(f, 200) is False


# ---------------------------------------------------------------------------
# _format_time
# ---------------------------------------------------------------------------

class TestFormatTime:
    @pytest.mark.parametrize("secs,expected", [
        (-1.0, "—"),
        (0.0, "0s"),
        (5.0, "5s"),
        (59.0, "59s"),
        (60.0, "1m 00s"),
        (90.0, "1m 30s"),
        (125.0, "2m 05s"),
    ])
    def test_formatting(self, secs, expected):
        from comfy_runner.environment import _format_time
        assert _format_time(secs) == expected


# ---------------------------------------------------------------------------
# read_manifest
# ---------------------------------------------------------------------------

class TestReadManifest:
    def test_reads_valid_json(self, tmp_path):
        from comfy_runner.environment import read_manifest
        data = {"version": "1.0", "variant": "nvidia"}
        (tmp_path / "manifest.json").write_text(json.dumps(data))
        assert read_manifest(tmp_path) == data

    def test_returns_none_when_missing(self, tmp_path):
        from comfy_runner.environment import read_manifest
        assert read_manifest(tmp_path) is None

    def test_returns_none_on_invalid_json(self, tmp_path):
        from comfy_runner.environment import read_manifest
        (tmp_path / "manifest.json").write_text("not json {{{")
        assert read_manifest(tmp_path) is None


# ---------------------------------------------------------------------------
# strip_master_packages / BULKY_PREFIXES
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ad-hoc build helpers
# ---------------------------------------------------------------------------

class TestGetPbsPlatform:
    def test_windows_amd64(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setattr("platform.machine", lambda: "AMD64")
        from comfy_runner.environment import _get_pbs_platform
        assert _get_pbs_platform() == "x86_64-pc-windows-msvc"

    def test_linux_x86_64(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        from comfy_runner.environment import _get_pbs_platform
        assert _get_pbs_platform() == "x86_64-unknown-linux-gnu"

    def test_darwin_arm64(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        from comfy_runner.environment import _get_pbs_platform
        assert _get_pbs_platform() == "aarch64-apple-darwin"

    def test_unsupported_raises(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "FreeBSD")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        from comfy_runner.environment import _get_pbs_platform
        with pytest.raises(RuntimeError, match="No python-build-standalone"):
            _get_pbs_platform()


class TestTorchvisionVer:
    def test_torch_2_10_0(self):
        from comfy_runner.environment import _torchvision_ver
        assert _torchvision_ver("2.10.0") == "0.25.0"

    def test_torch_2_5_1(self):
        from comfy_runner.environment import _torchvision_ver
        assert _torchvision_ver("2.5.1") == "0.20.1"

    def test_torch_2_0_0(self):
        from comfy_runner.environment import _torchvision_ver
        assert _torchvision_ver("2.0.0") == "0.15.0"

    def test_invalid_fallback(self):
        from comfy_runner.environment import _torchvision_ver
        assert _torchvision_ver("bad") == "0.25.0"


class TestResolveTorchPreset:
    def test_nvidia_default(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.environment.detect_gpu", lambda: "nvidia")
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset()
        assert "torch==2.10.0+cu130" in packages
        assert "torchvision==0.25.0+cu130" in packages
        assert "torchaudio==2.10.0+cu130" in packages
        assert index_url == "https://download.pytorch.org/whl/cu130"

    def test_cuda_tag_override(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(cuda_tag="cu128")
        assert "torch==2.10.0+cu128" in packages
        assert index_url == "https://download.pytorch.org/whl/cu128"

    def test_cuda_tag_custom(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(cuda_tag="cu124")
        assert "torch==2.10.0+cu124" in packages
        assert index_url == "https://download.pytorch.org/whl/cu124"

    def test_mps(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(gpu="mps")
        assert "torch==2.10.0" in packages
        assert index_url is None

    def test_cpu(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(gpu="cpu")
        assert "torch==2.10.0+cpu" in packages
        assert index_url == "https://download.pytorch.org/whl/cpu"

    def test_custom_torch_version(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, _ = _resolve_torch_preset(gpu="nvidia", torch_version="2.5.1")
        assert "torch==2.5.1+cu130" in packages
        assert "torchvision==0.20.1+cu130" in packages
        assert "torchaudio==2.5.1+cu130" in packages

    def test_amd(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(gpu="amd")
        assert "torch==2.10.0+rocm7.1" in packages
        assert index_url == "https://download.pytorch.org/whl/rocm7.1"

    def test_intel(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(gpu="intel")
        assert "torch==2.10.0+xpu" in packages
        assert index_url == "https://download.pytorch.org/whl/xpu"

    def test_rocm_cuda_tag(self):
        from comfy_runner.environment import _resolve_torch_preset
        packages, index_url = _resolve_torch_preset(cuda_tag="rocm7.1")
        assert "torch==2.10.0+rocm7.1" in packages
        assert index_url == "https://download.pytorch.org/whl/rocm7.1"


class TestStripBuild:
    def test_removes_expected_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import _strip_build
        sp = tmp_path / "lib" / "python3.13" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "torch" / "include" / "header.h").parent.mkdir(parents=True)
        (sp / "torch" / "include" / "header.h").touch()
        (sp / "torch" / "share" / "cmake").parent.mkdir(parents=True)
        (sp / "torch" / "share" / "cmake").touch()
        (sp / "torch" / "test" / "test_foo.py").parent.mkdir(parents=True)
        (sp / "torch" / "test" / "test_foo.py").touch()
        (sp / "caffe2" / "data.bin").parent.mkdir(parents=True)
        (sp / "caffe2" / "data.bin").touch()
        # This should survive
        (sp / "torch" / "nn" / "module.py").parent.mkdir(parents=True)
        (sp / "torch" / "nn" / "module.py").touch()
        (sp / "requests" / "__init__.py").parent.mkdir(parents=True)
        (sp / "requests" / "__init__.py").touch()

        _strip_build(tmp_path)

        assert not (sp / "torch" / "include").exists()
        assert not (sp / "torch" / "share").exists()
        assert not (sp / "torch" / "test").exists()
        assert not (sp / "caffe2").exists()
        assert (sp / "torch" / "nn" / "module.py").exists()
        assert (sp / "requests" / "__init__.py").exists()


class TestStripMasterPackages:
    def test_removes_bulky_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.platform", "linux")
        from comfy_runner.environment import strip_master_packages
        sp = tmp_path / "standalone-env" / "lib" / "python3.12" / "site-packages"
        sp.mkdir(parents=True)
        # Create dirs that should be removed
        (sp / "torch").mkdir()
        (sp / "torch" / "data.bin").touch()
        (sp / "nvidia_cublas").mkdir()
        (sp / "triton").mkdir()
        (sp / "cuda_runtime").mkdir()
        # Create dir that should survive
        (sp / "requests").mkdir()

        strip_master_packages(tmp_path)

        remaining = [e.name for e in sp.iterdir()]
        assert "requests" in remaining
        assert "torch" not in remaining
        assert "nvidia_cublas" not in remaining
        assert "triton" not in remaining
        assert "cuda_runtime" not in remaining
