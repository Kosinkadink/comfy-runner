"""Log file rotation and reading utilities.

Each ComfyUI boot gets its own log file.  On start the current log is
rotated to a timestamped name, a fresh file is opened, and old rotated
files beyond ``max_files`` are pruned.  This mirrors Desktop 1.0's
``rotateLogFiles`` pattern.

Log file naming:
  .comfy-runner.log               — current session
  .comfy-runner_2026-03-30T14-23-15.log  — rotated historical session
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_FILENAME = ".comfy-runner.log"
_ROTATED_RE = re.compile(r"^\.comfy-runner_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.log$")
MAX_LOG_FILES = 50


def _log_path(install_path: str | Path) -> Path:
    return Path(install_path) / LOG_FILENAME


def _rotated_logs(install_path: str | Path) -> list[Path]:
    """Return rotated log files sorted oldest-first."""
    parent = Path(install_path)
    files = [
        f for f in parent.iterdir()
        if f.is_file() and _ROTATED_RE.match(f.name)
    ]
    files.sort(key=lambda f: f.name)
    return files


def rotate_log(install_path: str | Path, max_files: int = MAX_LOG_FILES) -> None:
    """Rotate the current log file to a timestamped name.

    If there are more than *max_files* rotated logs, the oldest is deleted.
    """
    current = _log_path(install_path)
    if not current.exists() or current.stat().st_size == 0:
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    rotated = Path(install_path) / f".comfy-runner_{ts}.log"
    # Avoid collision if rotating multiple times in the same second
    if rotated.exists():
        rotated = Path(install_path) / f".comfy-runner_{ts}-1.log"

    try:
        current.rename(rotated)
    except OSError:
        # On Windows the file may be locked by a previous process;
        # fall back to truncate-on-open instead
        return

    # Prune old rotated files
    if max_files > 0:
        old = _rotated_logs(install_path)
        while len(old) > max_files:
            try:
                old[0].unlink()
            except OSError:
                pass
            old.pop(0)


def open_log(install_path: str | Path) -> tuple[Any, Path]:
    """Rotate the previous log and open a fresh one for writing.

    Returns ``(file_handle, log_path)``.
    """
    rotate_log(install_path)
    log_path = _log_path(install_path)
    fh = open(log_path, "w", encoding="utf-8")
    return fh, log_path


def read_current_log(
    install_path: str | Path,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """Read the current session's log file.

    Returns ``{"lines": [...], "size": <bytes>, "path": "..."}``.
    If *max_lines* is set, returns only the last N lines.
    """
    log_path = _log_path(install_path)
    if not log_path.exists():
        return {"lines": [], "size": 0, "path": str(log_path)}

    content = log_path.read_text(encoding="utf-8", errors="replace")
    all_lines = content.splitlines()
    size = log_path.stat().st_size

    if max_lines and len(all_lines) > max_lines:
        lines = all_lines[-max_lines:]
    else:
        lines = all_lines

    return {"lines": lines, "size": size, "path": str(log_path)}


def read_log_after(
    install_path: str | Path,
    after: int,
) -> dict[str, Any]:
    """Read new log content after byte offset *after*.

    Returns ``{"lines": [...], "offset": <new_offset>, "size": <total_size>}``.
    Used for incremental polling.
    """
    log_path = _log_path(install_path)
    if not log_path.exists():
        return {"lines": [], "offset": 0, "size": 0}

    size = log_path.stat().st_size
    if after >= size:
        return {"lines": [], "offset": size, "size": size}

    with open(log_path, "rb") as f:
        f.seek(after)
        raw = f.read()
        new_offset = f.tell()

    new_content = raw.decode("utf-8", errors="replace")
    lines = new_content.splitlines()
    return {"lines": lines, "offset": new_offset, "size": size}


def list_log_sessions(install_path: str | Path) -> list[dict[str, Any]]:
    """List all log sessions (current + rotated), newest first."""
    sessions = []
    current = _log_path(install_path)
    if current.exists():
        stat = current.stat()
        sessions.append({
            "filename": current.name,
            "current": True,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    for f in reversed(_rotated_logs(install_path)):
        stat = f.stat()
        sessions.append({
            "filename": f.name,
            "current": False,
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    return sessions
