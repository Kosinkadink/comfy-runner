"""macOS-specific binary repair — quarantine removal and codesigning.

Mirrors ComfyUI-Launcher standalone.ts: removeQuarantine, codesignBinaries,
isMachO, checkAndSign.
Only active on Darwin; all functions are no-ops on other platforms.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

# Mach-O magic numbers (big-endian representation of first 4 bytes)
_MACHO_MAGICS = {
    0xFEEDFACE,  # MH_MAGIC
    0xCEFAEDFE,  # MH_CIGAM
    0xFEEDFACF,  # MH_MAGIC_64
    0xCFFAEDFE,  # MH_CIGAM_64
    0xCAFEBABE,  # FAT_MAGIC
    0xBEBAFECA,  # FAT_CIGAM
}

# Extensions that are definitely not native binaries — skip them
# Mirrors standalone.ts NON_BINARY_EXTENSIONS
_NON_BINARY_EXTENSIONS = frozenset({
    ".py", ".pyc", ".pyo", ".pyi", ".pyd",
    ".txt", ".md", ".rst", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".csv",
    ".html", ".htm", ".css", ".js", ".ts", ".xml", ".svg",
    ".h", ".c", ".cpp", ".hpp", ".pxd", ".pyx",
    ".sh", ".bat", ".ps1",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".egg-info", ".dist-info", ".data",
    ".typed", ".license",
})

_CODESIGN_CONCURRENCY = 8


def remove_quarantine(
    directory: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Remove com.apple.quarantine extended attribute recursively.

    No-op on non-Darwin platforms.
    """
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(directory)],
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if send_output:
            send_output(f"⚠ removeQuarantine: {e}\n")


def codesign_binaries(
    directory: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Ad-hoc codesign all Mach-O binaries under a directory.

    Mirrors standalone.ts codesignBinaries — walks the tree, skips
    known non-binary extensions, checks Mach-O magic bytes, and
    runs `codesign --force --sign -` with bounded concurrency.

    No-op on non-Darwin platforms.
    """
    if sys.platform != "darwin":
        return

    candidates: list[Path] = []
    for item in directory.rglob("*"):
        if not item.is_file():
            continue
        if item.suffix.endswith(".dylib") or item.suffix.endswith(".so"):
            candidates.append(item)
        elif not _has_non_binary_extension(item.name):
            candidates.append(item)

    # Thread-safe output: collect warnings, emit after pool completes
    warnings: list[str] = []
    warnings_lock = threading.Lock()

    def _check_and_sign(file_path: Path) -> None:
        name = file_path.name
        if not name.endswith(".dylib") and not name.endswith(".so"):
            if not _is_macho(file_path):
                return
        try:
            result = subprocess.run(
                ["codesign", "--force", "--sign", "-", str(file_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                msg = f"⚠ codesign failed: {file_path}: {result.stderr.strip()}\n"
                with warnings_lock:
                    warnings.append(msg)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            msg = f"⚠ codesign failed: {file_path}: {e}\n"
            with warnings_lock:
                warnings.append(msg)

    with ThreadPoolExecutor(max_workers=_CODESIGN_CONCURRENCY) as pool:
        list(pool.map(_check_and_sign, candidates))

    if send_output:
        for msg in warnings:
            send_output(msg)


def repair_mac_binaries(
    install_path: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Full macOS binary repair: quarantine removal + codesigning.

    Mirrors standalone.ts repairMacBinaries. Call after extracting
    the standalone environment and after creating envs.

    No-op on non-Darwin platforms.
    """
    if sys.platform != "darwin":
        return

    standalone_env = install_path / "standalone-env"
    if standalone_env.exists():
        if send_output:
            send_output("Removing quarantine flags...\n")
        remove_quarantine(standalone_env, send_output)
        if send_output:
            send_output("Codesigning binaries...\n")
        codesign_binaries(standalone_env, send_output)

    from .environment import get_active_venv_dir
    env_dir = get_active_venv_dir(install_path)
    if env_dir is not None:
        if send_output:
            send_output("Codesigning environment binaries...\n")
        remove_quarantine(env_dir, send_output)
        codesign_binaries(env_dir, send_output)


def _has_non_binary_extension(name: str) -> bool:
    """Check if a filename has a known non-binary extension."""
    dot = name.rfind(".")
    if dot == -1:
        return False
    return name[dot:].lower() in _NON_BINARY_EXTENSIONS


def _is_macho(file_path: Path) -> bool:
    """Check if a file is a Mach-O binary by reading its magic bytes."""
    try:
        with open(file_path, "rb") as f:
            data = f.read(4)
        if len(data) < 4:
            return False
        magic = struct.unpack(">I", data)[0]
        return magic in _MACHO_MAGICS
    except OSError:
        return False
