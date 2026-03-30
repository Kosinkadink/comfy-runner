"""Tests for comfy_runner.pip_utils module."""

from __future__ import annotations

import pytest

from comfy_runner.pip_utils import PYTORCH_RE, is_protected_package


# ---------------------------------------------------------------------------
# PYTORCH_RE
# ---------------------------------------------------------------------------

class TestPytorchRe:
    @pytest.mark.parametrize("line", [
        "torch",
        "torch>=2.0",
        "torch==2.1.0",
        "torch<3",
        "torch~=2.0",
        "torch!=1.0",
        "torchvision",
        "torchvision>=0.15",
        "torchaudio",
        "torchaudio==2.1",
        "torchsde",
        "torchsde>=0.2",
        "TORCH",
        "Torch>=2.0",
        "torch[extra]",
        "torch;python_version>='3.8'",
        "torch # comment",
    ])
    def test_matches_pytorch_packages(self, line: str) -> None:
        assert PYTORCH_RE.match(line.strip()), f"Should match: {line!r}"

    @pytest.mark.parametrize("line", [
        "torch-fidelity",
        "torchmetrics",
        "requests",
        "numpy",
        "pytorch-lightning",
        "safetensors",
    ])
    def test_rejects_non_pytorch(self, line: str) -> None:
        assert not PYTORCH_RE.match(line.strip()), f"Should not match: {line!r}"


# ---------------------------------------------------------------------------
# is_protected_package
# ---------------------------------------------------------------------------

class TestIsProtectedPackage:
    @pytest.mark.parametrize("name", [
        "pip",
        "setuptools",
        "wheel",
        "uv",
        "torch",
        "torch-cuda",
        "torch_cuda",
        "nvidia-cudnn",
        "nvidia_cublas",
        "triton",
        "triton-nightly",
        "cuda-runtime",
        "cuda_toolkit",
    ])
    def test_protected(self, name: str) -> None:
        assert is_protected_package(name) is True

    @pytest.mark.parametrize("name", [
        "requests",
        "numpy",
        "pillow",
        "safetensors",
        "torchvision",
        "transformers",
        "accelerate",
    ])
    def test_not_protected(self, name: str) -> None:
        assert is_protected_package(name) is False
