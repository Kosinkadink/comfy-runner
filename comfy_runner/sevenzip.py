"""Auto-download standalone 7-Zip binaries for fast archive extraction.

Downloads a platform-appropriate 7z binary to ~/.comfy-runner/bin/ on
first use.  Subsequent calls return the cached path instantly.

Binaries are from the official 7-Zip project (https://7-zip.org/).
License: LGPL-2.1+ / BSD-3-Clause (since 7-Zip 23.01).
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import requests

from .config import CONFIG_DIR

_BIN_DIR = CONFIG_DIR / "bin"

# 7-Zip 26.00 (2026-02-12) — latest stable
_VERSION = "26.00"
_TAG = "2600"

_DOWNLOADS: dict[str, dict[str, Any]] = {
    # Windows: standalone 7zr.exe (single file, no DLLs, handles 7z + multi-volume)
    "win-x64": {
        "url": f"https://github.com/ip7z/7zip/releases/download/{_VERSION}/7zr.exe",
        "binary": "7zr.exe",
        "is_archive": False,
    },
    # Linux x86_64: tar.xz containing 7zz binary
    "linux-x64": {
        "url": f"https://github.com/ip7z/7zip/releases/download/{_VERSION}/7z{_TAG}-linux-x64.tar.xz",
        "binary": "7zz",
        "is_archive": True,
    },
    # Linux aarch64: tar.xz containing 7zz binary
    "linux-arm64": {
        "url": f"https://github.com/ip7z/7zip/releases/download/{_VERSION}/7z{_TAG}-linux-arm64.tar.xz",
        "binary": "7zz",
        "is_archive": True,
    },
    # macOS (universal): tar.xz containing 7zz binary
    "mac": {
        "url": f"https://github.com/ip7z/7zip/releases/download/{_VERSION}/7z{_TAG}-mac.tar.xz",
        "binary": "7zz",
        "is_archive": True,
    },
}


def _get_platform_key() -> str | None:
    """Return the download key for the current platform."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "win-x64"
    elif sys.platform == "darwin":
        return "mac"
    elif sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x64"
        elif machine in ("aarch64", "arm64"):
            return "linux-arm64"
    return None


def get_bundled_7z() -> str | None:
    """Return the path to the bundled 7z binary, or None if not downloaded yet."""
    key = _get_platform_key()
    if not key:
        return None
    info = _DOWNLOADS[key]
    binary_path = _BIN_DIR / info["binary"]
    if binary_path.exists():
        return str(binary_path)
    return None


def ensure_7z(
    send_output: Callable[[str], None] | None = None,
) -> str | None:
    """Ensure a 7z binary is available. Downloads if needed. Returns path or None."""
    # Check if already downloaded
    existing = get_bundled_7z()
    if existing:
        return existing

    key = _get_platform_key()
    if not key:
        return None

    info = _DOWNLOADS[key]
    url = info["url"]
    binary_name = info["binary"]
    is_archive = info["is_archive"]
    dest = _BIN_DIR / binary_name

    _BIN_DIR.mkdir(parents=True, exist_ok=True)

    if send_output:
        send_output(f"Downloading 7-Zip binary for fast extraction...\n")

    try:
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()

        if is_archive:
            # tar.xz archive — extract the 7zz binary
            data = resp.content
            with tarfile.open(fileobj=BytesIO(data), mode="r:xz") as tar:
                # Find the 7zz binary in the archive
                for member in tar.getmembers():
                    if member.name == binary_name or member.name.endswith(f"/{binary_name}"):
                        f = tar.extractfile(member)
                        if f:
                            dest.write_bytes(f.read())
                            break
                else:
                    if send_output:
                        send_output(f"⚠ Could not find {binary_name} in archive\n")
                    return None
        else:
            # Direct binary download (Windows 7zr.exe)
            dest.write_bytes(resp.content)

        # Make executable on Unix
        if sys.platform != "win32":
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        if send_output:
            send_output(f"7-Zip binary installed to {dest}\n")

        return str(dest)

    except Exception as e:
        if send_output:
            send_output(f"⚠ Failed to download 7-Zip binary: {e}\n")
        # Clean up partial download
        dest.unlink(missing_ok=True)
        return None
