"""Safe file I/O — atomic writes with backup and Windows retry.

Mirrors the pattern from ComfyUI-Launcher's safe-file.ts:
- Write to .tmp, optionally back up to .bak, then rename.
- On Windows, retry rename on EPERM/EACCES (antivirus/indexer locks).
- Read with .bak fallback if primary is missing or corrupt.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def atomic_write(path: Path, content: str, *, backup: bool = False) -> None:
    """Write content to a file atomically via temp-file + rename.

    Prevents corruption if the process is killed mid-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak_path = str(path) + ".bak"

    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # Backup existing file before replacing
        if backup and path.exists():
            try:
                import shutil
                shutil.copy2(str(path), bak_path)
            except OSError:
                pass

        # os.replace atomically replaces the target (uses MoveFileEx on Windows)
        # Retry on PermissionError — antivirus/indexer can briefly lock files
        retries = 3
        delay = 0.1
        for attempt in range(retries + 1):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError:
                if attempt < retries:
                    time.sleep(delay * (attempt + 1))
                else:
                    raise
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_read(path: Path) -> str | None:
    """Read a file, falling back to .bak if primary is missing or corrupt.

    If the backup is used, it is automatically restored as the primary.
    """
    path = Path(path)
    bak_path = Path(str(path) + ".bak")

    try:
        data = path.read_text(encoding="utf-8")
        if data:
            return data
    except OSError:
        pass

    try:
        data = bak_path.read_text(encoding="utf-8")
        if data:
            try:
                import shutil
                shutil.copy2(str(bak_path), str(path))
            except OSError:
                pass
            return data
    except OSError:
        pass

    return None


def is_safe_path_component(name: str) -> bool:
    """Check that *name* is a single safe filename component (no slashes, no `.` / `..`)."""
    return bool(name) and name == Path(name).name and name not in (".", "..")
