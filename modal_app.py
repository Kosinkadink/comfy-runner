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
SERVER_PORT = 9189

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
