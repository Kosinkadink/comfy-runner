"""Tests for comfy_runner.macos — extension checks, Mach-O detection, no-ops on Linux."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from comfy_runner.macos import (
    _has_non_binary_extension,
    _is_macho,
)


# ---------------------------------------------------------------------------
# _has_non_binary_extension
# ---------------------------------------------------------------------------

class TestHasNonBinaryExtension:
    @pytest.mark.parametrize("name", [
        "script.py", "data.json", "readme.txt", "config.yaml",
        "page.html", "code.c", "header.h", "run.sh",
        "image.png", "photo.jpg", "icon.ico",
    ])
    def test_returns_true_for_non_binary(self, name: str):
        assert _has_non_binary_extension(name) is True

    @pytest.mark.parametrize("name", [
        "libfoo.so",
        "libbar.dylib",
    ])
    def test_returns_false_for_binary_extensions(self, name: str):
        # .so and .dylib are NOT in _NON_BINARY_EXTENSIONS
        assert _has_non_binary_extension(name) is False

    def test_returns_false_for_no_extension(self):
        assert _has_non_binary_extension("python3") is False

    def test_case_insensitive(self):
        assert _has_non_binary_extension("FILE.PY") is True
        assert _has_non_binary_extension("FILE.JSON") is True


# ---------------------------------------------------------------------------
# _is_macho
# ---------------------------------------------------------------------------

class TestIsMacho:
    def test_false_for_random_bytes(self, tmp_path: Path):
        f = tmp_path / "random.bin"
        f.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07")
        assert _is_macho(f) is False

    def test_false_for_small_file(self, tmp_path: Path):
        f = tmp_path / "tiny"
        f.write_bytes(b"\x00\x01")
        assert _is_macho(f) is False

    def test_true_for_macho_magic(self, tmp_path: Path):
        f = tmp_path / "fake_macho"
        # MH_MAGIC_64 = 0xFEEDFACF
        magic = struct.pack(">I", 0xFEEDFACF)
        f.write_bytes(magic + b"\x00" * 100)
        assert _is_macho(f) is True

    def test_false_for_nonexistent(self, tmp_path: Path):
        f = tmp_path / "does_not_exist"
        assert _is_macho(f) is False
