"""Workflow template model parsing and downloading.

Parses ComfyUI workflow template JSON files and downloads referenced models.
Each workflow node may declare required models in properties.models; this module
extracts, deduplicates, checks, and downloads them.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import requests

from .config import get_shared_dir


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_workflow_models(workflow: dict[str, Any]) -> list[dict[str, str]]:
    """Extract all model entries from workflow nodes, deduplicated by (name, directory).

    Each entry has keys: name, url, directory.
    """
    seen: set[tuple[str, str]] = set()
    models: list[dict[str, str]] = []

    for node in workflow.get("nodes", []):
        node_models = (node.get("properties") or {}).get("models")
        if not isinstance(node_models, list):
            continue
        for entry in node_models:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            url = entry.get("url", "")
            directory = entry.get("directory", "")
            if not name or not url or not directory:
                continue
            key = (name, directory)
            if key in seen:
                continue
            seen.add(key)
            models.append({"name": name, "url": url, "directory": directory})

    return models


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_models_dir(install_path: str | Path) -> Path:
    """Return the models root directory for an installation.

    If a shared_dir is configured, uses ``{shared_dir}/models``.
    Otherwise falls back to ``{install_path}/ComfyUI/models``.
    """
    shared_dir = get_shared_dir()
    if shared_dir:
        return Path(shared_dir).resolve() / "models"
    return Path(install_path) / "ComfyUI" / "models"


# ---------------------------------------------------------------------------
# Check which models are missing
# ---------------------------------------------------------------------------

def _validate_model_path(models_dir: Path, directory: str, name: str) -> Path:
    """Resolve and validate a model path, preventing path traversal.

    Raises ValueError if the resolved path escapes models_dir.
    """
    resolved = (models_dir / directory / name).resolve()
    if not resolved.is_relative_to(models_dir.resolve()):
        raise ValueError(f"Invalid model path: {directory}/{name}")
    return resolved


def _get_auth_headers(url: str) -> dict[str, str]:
    """Return auth headers for known model hosting platforms.

    Detects HuggingFace and ModelScope URLs and adds Bearer token
    if configured. Returns empty dict for unknown hosts or if no
    token is set.
    """
    from urllib.parse import urlparse
    from .config import get_hf_token, get_modelscope_token

    host = urlparse(url).hostname or ""
    if "huggingface.co" in host:
        token = get_hf_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
    elif "modelscope.cn" in host or "modelscope.ai" in host:
        token = get_modelscope_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
    return {}


def check_missing_models(
    models: list[dict[str, str]],
    models_dir: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Partition models into (missing, existing) lists.

    A model exists if ``{models_dir}/{directory}/{name}`` is a file.
    """
    missing: list[dict[str, str]] = []
    existing: list[dict[str, str]] = []

    for model in models:
        path = _validate_model_path(models_dir, model["directory"], model["name"])
        if path.is_file():
            existing.append(model)
        else:
            missing.append(model)

    return missing, existing


# ---------------------------------------------------------------------------
# Size formatting
# ---------------------------------------------------------------------------

def _format_size(n: int) -> str:
    """Format byte count as human-readable string (e.g. '1.2 GB')."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.1f} GB"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 8192
_PROGRESS_BYTES = 10 * 1024 * 1024  # 10 MB


def cleanup_staging(models_dir: Path) -> int:
    """Remove the legacy ``{models_dir}/.staging/`` directory if present.

    Returns the number of files removed.  Downloads now use a temp directory
    outside ``models_dir``, but this cleans up leftovers from older versions.
    """
    staging_dir = models_dir / ".staging"
    if not staging_dir.is_dir():
        return 0
    count = 0
    for part_file in staging_dir.glob("*.part"):
        try:
            part_file.unlink()
            count += 1
        except OSError:
            pass
    # Remove the directory itself if empty
    try:
        staging_dir.rmdir()
    except OSError:
        pass
    return count


def cleanup_staging_all(
    send_output: Callable[[str], None] | None = None,
) -> int:
    """Clean up staging files across all installations.

    Returns total number of files removed.
    """
    from .installations import show_list

    out = send_output or (lambda _: None)
    total_removed = 0
    for inst in show_list():
        install_path = inst.get("path")
        if not install_path:
            continue
        models_dir = resolve_models_dir(install_path)
        count = cleanup_staging(models_dir)
        if count:
            out(f"Cleaned up {count} staging file(s) in {inst['name']}\n")
            total_removed += count
    return total_removed


def download_models(
    models: list[dict[str, str]],
    models_dir: Path,
    send_output: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Download models to ``{models_dir}/{directory}/{name}``.

    Downloads are staged in a temporary directory outside ``models_dir`` as
    ``.part`` files and moved to the final location on completion.  Reports
    progress every ~10% or every 10 MB.

    If *cancel_event* is provided and becomes set, the current download is
    aborted, its temp file removed, and the result includes
    ``"cancelled": True``.

    Returns ``{"downloaded": [...], "skipped": [...], "failed": [...], "errors": [...]}``.
    """
    out = send_output or (lambda _: None)
    result: dict[str, Any] = {
        "downloaded": [],
        "skipped": [],
        "failed": [],
        "errors": [],
    }
    total = len(models)
    staging_tmpdir = tempfile.mkdtemp(prefix="comfy-dl-staging-")
    staging_dir = Path(staging_tmpdir)

    try:
        for idx, model in enumerate(models, 1):
            rel = f"{model['directory']}/{model['name']}"
            dest_file = _validate_model_path(models_dir, model["directory"], model["name"])
            dest_dir = dest_file.parent

            # Check cancellation before starting a new model
            if cancel_event is not None and cancel_event.is_set():
                result["cancelled"] = True
                return result

            # Skip if already exists
            if dest_file.is_file():
                result["skipped"].append(rel)
                out(f"  [{idx}/{total}] {rel}  skipped (exists)\n")
                continue

            try:
                auth_headers = _get_auth_headers(model["url"])
                resp = requests.get(model["url"], stream=True, timeout=30, headers=auth_headers)
                resp.raise_for_status()
            except Exception as e:
                result["failed"].append(rel)
                result["errors"].append(f"{rel}: {e}")
                out(f"  [{idx}/{total}] {rel}  FAILED ({e})\n")
                continue

            content_length = resp.headers.get("Content-Length")
            total_size = int(content_length) if content_length else None

            # Determine progress thresholds
            pct_step = (total_size // 10) if total_size else None

            downloaded = 0
            last_report = 0
            chunk_count = 0
            cancelled = False

            # Write to staging file, then move to final destination
            staging_name = f"{model['directory']}--{model['name']}.part"
            tmp_path = staging_dir / staging_name
            try:
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                        f.write(chunk)
                        downloaded += len(chunk)
                        chunk_count += 1

                        # Check cancellation every 100 chunks (~800 KB)
                        if cancel_event is not None and chunk_count % 100 == 0:
                            if cancel_event.is_set():
                                cancelled = True
                                break

                        # Report progress
                        should_report = False
                        if pct_step and pct_step > 0:
                            if downloaded - last_report >= pct_step:
                                should_report = True
                        if downloaded - last_report >= _PROGRESS_BYTES:
                            should_report = True

                        if should_report:
                            last_report = downloaded
                            if total_size:
                                pct = int(downloaded * 100 / total_size)
                                out(
                                    f"  [{idx}/{total}] {rel}"
                                    f"  {pct}%"
                                    f" ({_format_size(downloaded)} / {_format_size(total_size)})\n"
                                )
                            else:
                                out(
                                    f"  [{idx}/{total}] {rel}"
                                    f"  {_format_size(downloaded)}\n"
                                )

                if cancelled:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    result["cancelled"] = True
                    return result

                # Move from staging to final destination
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_path), str(dest_file))
                result["downloaded"].append(rel)
                out(f"  [{idx}/{total}] {rel}  done ({_format_size(downloaded)})\n")

            except Exception as e:
                result["failed"].append(rel)
                result["errors"].append(f"{rel}: {e}")
                out(f"  [{idx}/{total}] {rel}  FAILED ({e})\n")
                # Clean up temp file
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        return result
    finally:
        shutil.rmtree(staging_tmpdir, ignore_errors=True)
