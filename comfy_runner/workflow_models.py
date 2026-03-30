"""Workflow template model parsing and downloading.

Parses ComfyUI workflow template JSON files and downloads referenced models.
Each workflow node may declare required models in properties.models; this module
extracts, deduplicates, checks, and downloads them.
"""

from __future__ import annotations

import os
import tempfile
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
        path = models_dir / model["directory"] / model["name"]
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


def download_models(
    models: list[dict[str, str]],
    models_dir: Path,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Download models to ``{models_dir}/{directory}/{name}``.

    Creates target directories as needed.  Uses a temp file during download
    and renames on completion.  Reports progress every ~10% or every 10 MB.

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

    for idx, model in enumerate(models, 1):
        rel = f"{model['directory']}/{model['name']}"
        dest_dir = models_dir / model["directory"]
        dest_file = dest_dir / model["name"]

        # Skip if already exists
        if dest_file.is_file():
            result["skipped"].append(rel)
            out(f"  [{idx}/{total}] {rel}  skipped (exists)\n")
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            resp = requests.get(model["url"], stream=True, timeout=30)
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

        # Write to temp file in the same directory, then rename
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(dest_dir), prefix=f".{model['name']}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)

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

            # Atomic rename
            tmp_path.replace(dest_file)
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
