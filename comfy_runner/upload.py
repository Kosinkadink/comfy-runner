"""Model file upload with resumable staging and integrity verification.

Handles streaming uploads to a staging directory, with support for
resuming interrupted uploads. Staging files are `.part` files that
get atomically moved to the final models directory on completion.

Supports SHA-256 and BLAKE3 hash verification.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

import blake3 as _blake3

from .config import CONFIG_DIR

HASH_ALGORITHMS = ("sha256", "blake3")


STAGING_DIR = CONFIG_DIR / "upload-staging"
STALE_THRESHOLD_S = 24 * 60 * 60  # 24 hours


def _staging_key(directory: str, name: str) -> str:
    """Deterministic key for a staging file based on target path."""
    raw = f"{directory}/{name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _staging_path(directory: str, name: str) -> Path:
    """Return the `.part` staging path for a given model target."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    key = _staging_key(directory, name)
    return STAGING_DIR / f"{key}_{name}.part"


def _meta_path(directory: str, name: str) -> Path:
    """Return the metadata path alongside the `.part` file."""
    part = _staging_path(directory, name)
    return part.with_suffix(".meta")


def _write_meta(directory: str, name: str) -> None:
    """Write metadata for a staging file."""
    import json
    from safe_file import atomic_write

    meta = {
        "directory": directory,
        "name": name,
        "created_at": time.time(),
    }
    atomic_write(_meta_path(directory, name), json.dumps(meta) + "\n")


def _read_meta(meta_path: Path) -> dict[str, Any] | None:
    """Read metadata from a `.meta` file."""
    import json

    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cleanup_stale_staging(
    max_age_s: float = STALE_THRESHOLD_S,
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Remove staging files older than *max_age_s* seconds.

    Returns the number of files cleaned up.
    """
    if not STAGING_DIR.exists():
        return 0

    now = time.time()
    cleaned = 0

    for meta_file in STAGING_DIR.glob("*.meta"):
        meta = _read_meta(meta_file)
        if not meta:
            # Orphaned meta file — remove it
            meta_file.unlink(missing_ok=True)
            cleaned += 1
            continue

        age = now - meta.get("created_at", 0)
        if age > max_age_s:
            part_file = meta_file.with_suffix(".part")
            name = meta.get("name", "?")
            if send_output:
                send_output(f"  Removing stale upload: {name} ({age / 3600:.1f}h old)\n")
            part_file.unlink(missing_ok=True)
            meta_file.unlink(missing_ok=True)
            cleaned += 1

    # Also clean orphaned .part files with no .meta
    for part_file in STAGING_DIR.glob("*.part"):
        if not part_file.with_suffix(".meta").exists():
            age = now - part_file.stat().st_mtime
            if age > max_age_s:
                if send_output:
                    send_output(f"  Removing orphaned staging file: {part_file.name}\n")
                part_file.unlink(missing_ok=True)
                cleaned += 1

    return cleaned


def get_upload_status(
    models_dir: Path, directory: str, name: str,
) -> dict[str, Any]:
    """Check status of a partial upload or existing file.

    Returns dict with keys: exists, bytes_received, complete, path, created_at.
    """
    from .workflow_models import _validate_model_path

    final_path = _validate_model_path(models_dir, directory, name)

    if final_path.is_file():
        return {
            "exists": True,
            "complete": True,
            "bytes_received": final_path.stat().st_size,
            "path": f"{directory}/{name}",
        }

    part = _staging_path(directory, name)
    if part.is_file():
        meta = _read_meta(_meta_path(directory, name))
        return {
            "exists": True,
            "complete": False,
            "bytes_received": part.stat().st_size,
            "path": f"{directory}/{name}",
            "created_at": meta.get("created_at") if meta else None,
        }

    return {"exists": False, "bytes_received": 0, "complete": False}


def delete_staging(directory: str, name: str) -> bool:
    """Remove a partial upload's staging files.

    Returns True if files were removed, False if nothing existed.
    """
    part = _staging_path(directory, name)
    meta = _meta_path(directory, name)
    existed = part.exists() or meta.exists()
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return existed


def compute_file_hash(path: Path, algorithm: str = "blake3") -> str:
    """Compute the hash of a file on disk.

    *algorithm* must be one of ``HASH_ALGORITHMS``.
    """
    if algorithm not in HASH_ALGORITHMS:
        raise ValueError(f"Unsupported hash algorithm: {algorithm!r}. Use one of {HASH_ALGORITHMS}")

    chunk_size = 256 * 1024

    if algorithm == "blake3":
        h = _blake3.blake3()
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest()
    else:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
        return h.hexdigest()


def receive_upload(
    models_dir: Path,
    directory: str,
    name: str,
    stream: BinaryIO,
    offset: int = 0,
    expected_hash: str = "",
    hash_type: str = "blake3",
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Stream an upload to staging and move to final location on completion.

    *stream* is any file-like object (e.g. request.files['file']).
    *offset* is the byte position in the final file where this data starts
    (0 for new uploads, >0 for resumptions).
    *expected_hash* — if provided, the completed file's hash is verified
    before moving to the final location.  *hash_type* selects the algorithm
    (``sha256`` or ``blake3``, default ``blake3``).

    Returns dict with: path, size, resumed, hash, hash_type.
    """
    from .workflow_models import _validate_model_path

    final_path = _validate_model_path(models_dir, directory, name)

    if final_path.is_file():
        return {
            "path": f"{directory}/{name}",
            "size": final_path.stat().st_size,
            "skipped": True,
        }

    if expected_hash and hash_type not in HASH_ALGORITHMS:
        raise ValueError(f"Unsupported hash_type: {hash_type!r}. Use one of {HASH_ALGORITHMS}")

    # Clean stale uploads before starting a new one
    cleanup_stale_staging(send_output=send_output)

    part = _staging_path(directory, name)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    resumed = offset > 0 and part.is_file()

    if offset > 0 and part.is_file():
        current_size = part.stat().st_size
        if offset > current_size:
            raise ValueError(
                f"Offset {offset} exceeds existing partial size {current_size}"
            )
        if current_size != offset:
            with open(part, "r+b") as f:
                f.truncate(offset)
    elif offset == 0:
        part.unlink(missing_ok=True)

    out = send_output or (lambda _: None)

    mode = "ab" if offset > 0 else "wb"
    chunk_size = 256 * 1024

    with open(part, mode) as f:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)

    total_size = part.stat().st_size
    _write_meta(directory, name)

    if send_output:
        out(f"  Received {total_size / 1048576:.1f} MB\n")

    # Verify hash if provided
    if expected_hash:
        if send_output:
            out(f"  Verifying {hash_type} hash...\n")
        actual_hash = compute_file_hash(part, hash_type)
        if actual_hash != expected_hash.lower():
            part.unlink(missing_ok=True)
            _meta_path(directory, name).unlink(missing_ok=True)
            raise ValueError(
                f"Hash mismatch: expected {expected_hash}, got {actual_hash}. "
                f"Upload deleted."
            )
        if send_output:
            out(f"  ✓ Hash verified\n")

    # Compute hash for response (if not already computed)
    if not expected_hash:
        actual_hash = compute_file_hash(part, hash_type)
    else:
        actual_hash = expected_hash.lower()

    # Move to final location
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.rename(str(part), str(final_path))
    _meta_path(directory, name).unlink(missing_ok=True)

    if send_output:
        out(f"  ✓ Saved to {directory}/{name}\n")

    return {
        "path": f"{directory}/{name}",
        "size": total_size,
        "resumed": resumed,
        "hash": actual_hash,
        "hash_type": hash_type,
    }
