"""Modal deployment for comfy-runner server.

Runs comfy-runner server + ComfyUI inside a Modal container with a GPU.
From the outside it looks like any other remote comfy-runner server.

Usage:
    # Development (temporary URL)
    modal serve modal_app.py

    # Production (persistent URL)
    modal deploy modal_app.py

    # Override GPU type (default: L40S)
    GPU=H100 modal deploy modal_app.py

    # Pre-cache standalone env into volume (run once, no GPU needed)
    modal run modal_app.py::prefetch

Environment variables:
    GPU         GPU type (default: L40S). Options: T4, L4, A10, L40S,
                A100, A100-80GB, H100, H200, B200
    MIN_CONTAINERS  Minimum containers to keep warm (default: 0)
"""

from __future__ import annotations

import os
import sys

import modal

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GPU_TYPE = os.environ.get("GPU", "L40S")
KEEP_WARM = int(os.environ.get("MIN_CONTAINERS", "0"))

COMFY_RUNNER_PATH = "/opt/comfy-runner"
# Mount local source into the image (works for private repos without auth)
LOCAL_SOURCE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/data"

# ---------------------------------------------------------------------------
# Volumes — persist installations + model data across container restarts
# ---------------------------------------------------------------------------

volume = modal.Volume.from_name("comfy-runner-data", create_if_missing=True)

# ---------------------------------------------------------------------------
# Container image
# ---------------------------------------------------------------------------

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "git",
        "p7zip-full",       # native 7z for fast archive extraction
        "pciutils",         # lspci for GPU detection
        "procps",           # ps, etc.
    )
    .add_local_dir(LOCAL_SOURCE, COMFY_RUNNER_PATH, copy=True, ignore=["__pycache__", ".venv", ".git", "*.pyc"])
    .pip_install("requests", "py7zr", "multivolumefile", "rich", "flask", "waitress")
    # Set COMFY_RUNNER_HOME early so config.py picks it up at import time
    .env({"COMFY_RUNNER_HOME": f"{DATA_DIR}/.comfy-runner"})
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = modal.App("comfy-runner", image=image)


@app.cls(
    gpu=GPU_TYPE,
    volumes={DATA_DIR: volume},
    timeout=86400,          # 24h — long-running server
    min_containers=KEEP_WARM,
)
@modal.concurrent(max_inputs=16)
class ComfyRunnerServer:
    """Runs comfy-runner server inside a Modal container."""

    @modal.enter()
    def startup(self):
        """Add comfy-runner to sys.path so imports work."""
        if COMFY_RUNNER_PATH not in sys.path:
            sys.path.insert(0, COMFY_RUNNER_PATH)

    @modal.wsgi_app(requires_proxy_auth=True)
    def serve(self):
        """Return the Flask WSGI app for Modal to serve."""
        if COMFY_RUNNER_PATH not in sys.path:
            sys.path.insert(0, COMFY_RUNNER_PATH)

        from comfy_runner_server.server import create_app
        return create_app()


# ---------------------------------------------------------------------------
# One-shot prefetch: download + extract standalone env into the volume
# ---------------------------------------------------------------------------
# Usage: modal run modal_app.py::prefetch
# This runs on a cheap CPU container (no GPU) and pre-populates the
# persistent volume so the first deploy doesn't wait for the download.

@app.function(
    volumes={DATA_DIR: volume},
    timeout=1800,       # 30 min max
)
def prefetch():
    """Download and extract the standalone environment into the volume."""
    if COMFY_RUNNER_PATH not in sys.path:
        sys.path.insert(0, COMFY_RUNNER_PATH)

    from comfy_runner.environment import (
        fetch_latest_release,
        fetch_manifests,
        pick_variant,
    )

    def _log(msg):
        print(msg, end="", flush=True)

    _log("Fetching latest release...\n")
    release = fetch_latest_release()
    tag = release.get("tag_name", "?")
    _log(f"Release: {tag}\n")

    manifests = fetch_manifests(release)
    variant = pick_variant(manifests, release, variant_id="linux-nvidia")
    manifest = variant["manifest"]
    cache_key = f"{tag}_{manifest['id']}"

    _log(f"Variant: {manifest['id']}\n")
    _log(f"ComfyUI ref: {manifest.get('comfyui_ref', '?')}\n")

    download_files = variant["download_files"]
    total_mb = sum(f.get("size", 0) for f in download_files) / 1048576
    _log(f"Download size: {total_mb:.0f} MB\n\n")

    # Download archives to cache (volume-backed) without extracting.
    # The real init_installation will find them cached and only extract.
    from comfy_runner import cache as download_cache
    from comfy_runner.environment import _is_download_complete, _download_file_resumable
    import time as _time

    cache_dir = download_cache.get_cache_path(cache_key)
    total_bytes = sum(f.get("size", 0) for f in download_files)
    completed_bytes = 0
    overall_start = _time.monotonic()

    for i, finfo in enumerate(download_files, 1):
        filename = finfo["filename"]
        expected_size = finfo.get("size", 0)
        file_path = cache_dir / filename
        label = f" ({i}/{len(download_files)})" if len(download_files) > 1 else ""

        if _is_download_complete(file_path, expected_size):
            completed_bytes += expected_size
            _log(f"Already cached: {filename}{label}\n")
        else:
            size_mb = expected_size / 1048576 if expected_size else 0
            _log(f"Downloading {filename}{label} ({size_mb:.0f} MB)...\n")
            _download_file_resumable(
                finfo["url"], file_path, expected_size,
                base_completed=completed_bytes,
                total_bytes=total_bytes,
                overall_start=overall_start,
                send_output=_log,
            )
            completed_bytes += expected_size

    download_cache.touch(cache_key)
    _log("\nPrefetch complete. Archives cached in volume.\n")
    volume.commit()
