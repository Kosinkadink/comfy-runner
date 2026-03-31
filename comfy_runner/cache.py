"""Download cache for standalone environment archives.

Stores downloaded archives in ~/.comfy-runner/cache/ keyed by
release+variant so re-installs skip the download step.
Mirrors ComfyUI-Launcher's installer.ts cache pattern.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

CACHE_DIR = CONFIG_DIR / "cache"
CACHE_META_FILE = CACHE_DIR / "cache-meta.json"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB
DEFAULT_MAX_ENTRIES = 3  # keep only the last N releases


def get_cache_path(key: str) -> Path:
    """Return the cache directory for a given key, creating it if needed."""
    p = CACHE_DIR / key
    p.mkdir(parents=True, exist_ok=True)
    return p


def touch(key: str) -> None:
    """Mark a cache entry as recently used."""
    meta = _load_meta()
    meta[key] = {"last_used": time.time()}
    _save_meta(meta)


def evict(
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> None:
    """Evict oldest cache entries until within *max_entries* and *max_bytes*.

    Entries are sorted by last-used time (oldest first).  The entry-count
    limit is checked first, then the byte-budget limit.
    """
    if not CACHE_DIR.exists():
        return

    meta = _load_meta()

    # Build list of (key, dir_path, size, last_used)
    entries: list[tuple[str, Path, int, float]] = []
    for entry_dir in CACHE_DIR.iterdir():
        if not entry_dir.is_dir() or entry_dir.name == "__pycache__":
            continue
        key = entry_dir.name
        size = _dir_size(entry_dir)
        last_used = meta.get(key, {}).get("last_used", 0.0)
        entries.append((key, entry_dir, size, last_used))

    total = sum(e[2] for e in entries)
    count = len(entries)

    if count <= max_entries and total <= max_bytes:
        return

    # Sort by last_used ascending (oldest first)
    entries.sort(key=lambda e: e[3])

    import shutil
    for key, dir_path, size, _ in entries:
        if count <= max_entries and total <= max_bytes:
            break
        shutil.rmtree(dir_path, ignore_errors=True)
        meta.pop(key, None)
        total -= size
        count -= 1

    _save_meta(meta)


def _dir_size(path: Path) -> int:
    """Calculate total size of a directory."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def _load_meta() -> dict[str, Any]:
    try:
        return json.loads(CACHE_META_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_meta(meta: dict[str, Any]) -> None:
    try:
        from safe_file import atomic_write
        atomic_write(CACHE_META_FILE, json.dumps(meta, indent=2) + "\n")
    except OSError:
        pass
