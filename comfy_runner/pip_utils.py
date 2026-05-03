"""pip operations with PyTorch protection — mirrors ComfyUI-Launcher pip.ts."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .environment import get_active_python_path, get_uv_path

# Regex matching PyTorch-family packages that must never be overwritten.
# Mirrors pip.ts PYTORCH_RE.
PYTORCH_RE = re.compile(
    r"^(torch|torchvision|torchaudio|torchsde)(\s*[<>=!~;[#]|$)", re.IGNORECASE
)


# Protected packages — mirrors snapshots.ts isProtectedPackage
_PROTECTED_EXACT = {"pip", "setuptools", "wheel", "uv"}
_PROTECTED_PREFIXES = ("torch", "nvidia", "triton", "cuda")


def is_protected_package(name: str) -> bool:
    """Return True if a package should never be modified during restore."""
    lower = name.lower()
    if lower in _PROTECTED_EXACT:
        return True
    return any(
        lower == p or lower.startswith(p + "-") or lower.startswith(p + "_")
        for p in _PROTECTED_PREFIXES
    )


def run_uv_pip(
    install_path: str | Path,
    args: list[str],
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Run a uv pip command and stream output. Returns exit code."""
    install_path = Path(install_path)
    uv = get_uv_path(install_path)
    python = get_active_python_path(install_path)

    if not uv.exists():
        raise RuntimeError(f"uv not found at {uv}")
    if not python or not python.exists():
        raise RuntimeError(f"Python not found for installation at {install_path}")

    cmd = [str(uv), "pip", *args, "--python", str(python)]

    proc = subprocess.Popen(
        cmd,
        cwd=str(install_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if send_output:
            send_output(line)
    proc.wait()
    return proc.returncode


def install_filtered_requirements(
    install_path: str | Path,
    req_path: str | Path,
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Install requirements while filtering out PyTorch packages.

    Mirrors pip.ts installFilteredRequirements:
    1. Read requirements file
    2. Filter out lines matching PYTORCH_RE
    3. Write a temp file with the filtered contents
    4. Run uv pip install -r with the temp file
    5. Clean up the temp file

    Returns exit code (0 = success).
    """
    req_path = Path(req_path)
    if not req_path.exists():
        if send_output:
            send_output(f"Requirements file not found: {req_path}\n")
        return 0  # Nothing to install

    content = req_path.read_text(encoding="utf-8")
    filtered_lines = [
        line for line in content.splitlines() if not PYTORCH_RE.match(line.strip())
    ]
    filtered = "\n".join(filtered_lines) + "\n"

    # Skip if nothing left after filtering
    non_empty = [l for l in filtered_lines if l.strip() and not l.strip().startswith("#")]
    if not non_empty:
        if send_output:
            send_output("No non-PyTorch requirements to install.\n")
        return 0

    # Write filtered requirements to a temp file
    install_path = Path(install_path)
    tmp = install_path / f".comfy-runner-filtered-requirements-{os.getpid()}.txt"
    try:
        tmp.write_text(filtered, encoding="utf-8")
        if send_output:
            send_output(f"Installing requirements (PyTorch packages filtered)...\n")
        return run_uv_pip(
            install_path,
            ["install", "-r", str(tmp)],
            send_output=send_output,
        )
    finally:
        tmp.unlink(missing_ok=True)


# Files under ComfyUI/ that may pin runtime/manager dependencies and
# whose changes should trigger a pip install on deploy.
DEPLOY_REQUIREMENT_FILES: tuple[str, ...] = (
    "requirements.txt",
    "manager_requirements.txt",
)


def install_changed_requirements(
    install_path: str | Path,
    changed_files: list[str],
    send_output: Callable[[str], None] | None = None,
) -> bool:
    """Install any of `DEPLOY_REQUIREMENT_FILES` that changed during deploy.

    Each file is installed independently so a `manager_requirements.txt`-only
    change is not silently skipped. Returns True iff at least one file was
    installed and every install succeeded.
    """
    install_path = Path(install_path)
    comfyui_dir = install_path / "ComfyUI"

    changed_req_files = [f for f in DEPLOY_REQUIREMENT_FILES if f in changed_files]
    if not changed_req_files:
        return False

    if send_output:
        send_output("\nRequirements changed — installing dependencies...\n")

    installed_any = False
    install_ok = True
    for req_filename in changed_req_files:
        req_path = comfyui_dir / req_filename
        if not req_path.exists():
            continue
        rc = install_filtered_requirements(
            install_path, req_path, send_output=send_output
        )
        installed_any = True
        if rc != 0:
            install_ok = False
            if send_output:
                send_output(
                    f"⚠ pip install for {req_filename} exited with code {rc}\n"
                )

    return installed_any and install_ok


def pip_freeze(
    install_path: str | Path,
) -> dict[str, str]:
    """Run uv pip freeze and return a {package: version} dict.

    Mirrors pip.ts pipFreeze — handles editable installs, direct references,
    and standard package==version lines.
    """
    install_path = Path(install_path)
    uv = get_uv_path(install_path)
    python = get_active_python_path(install_path)
    if not python or not python.exists():
        raise RuntimeError(f"Python not found for installation at {install_path}")

    result = subprocess.run(
        [str(uv), "pip", "freeze", "--python", str(python)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(install_path),
        creationflags=subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[:500]
        raise RuntimeError(f"uv pip freeze failed: {detail}")

    packages: dict[str, str] = {}
    for line in result.stdout.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        # Editable installs: "-e git+https://...@commit#egg=name"
        if trimmed.startswith("-e "):
            m = re.search(r"#egg=(.+)", trimmed)
            if m:
                packages[m.group(1)] = trimmed
            continue
        # PEP 508 direct references: "package @ git+https://..."
        at_match = re.match(r"^([A-Za-z0-9_.-]+)\s*@\s*(.+)$", trimmed)
        if at_match:
            packages[at_match.group(1)] = at_match.group(2).strip()
            continue
        # Standard: "package==version"
        eq_idx = trimmed.find("==")
        if eq_idx > 0:
            packages[trimmed[:eq_idx]] = trimmed[eq_idx + 2 :]

    return packages
