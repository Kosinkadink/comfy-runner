"""Tests for comfy_runner.comfyui — _comfyui_dir, _changed_files."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_runner.comfyui import _changed_files, _comfyui_dir


class TestComfyuiDir:
    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="ComfyUI not found"):
            _comfyui_dir(tmp_path)

    def test_returns_path_when_exists(self, tmp_path: Path):
        (tmp_path / "ComfyUI").mkdir()
        result = _comfyui_dir(tmp_path)
        assert result == tmp_path / "ComfyUI"


class TestChangedFiles:
    def test_empty_when_same_head(self):
        assert _changed_files("/fake", "abc123", "abc123") == []
