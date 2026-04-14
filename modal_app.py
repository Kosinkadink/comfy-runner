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
    scaledown_window=1200,  # 20 min — keep alive during long ComfyUI boots
    min_containers=KEEP_WARM,
)
@modal.concurrent(max_inputs=16)
class ComfyRunnerServer:
    """Runs comfy-runner server inside a Modal container."""

    @modal.enter()
    def startup(self):
        """Add comfy-runner to sys.path and open a tunnel to ComfyUI."""
        if COMFY_RUNNER_PATH not in sys.path:
            sys.path.insert(0, COMFY_RUNNER_PATH)

        # Ensure ComfyUI listens on all interfaces (needed for tunnel).
        # Patch any existing installation records to include --listen 0.0.0.0.
        from comfy_runner.config import list_installations, set_installation
        for inst_name, record in list_installations().items():
            args = record.get("launch_args", "")
            if "--listen" not in args:
                record["launch_args"] = f"{args} --listen 0.0.0.0".strip()
                set_installation(inst_name, record)

        # Open a persistent tunnel to ComfyUI's port (8188).
        # The tunnel stays open for the container's lifetime.
        self._tunnel_ctx = modal.forward(8188)
        tunnel = self._tunnel_ctx.__enter__()
        self._comfyui_tunnel_url = tunnel.url
        # Store in env so the Flask app can read it
        os.environ["COMFYUI_TUNNEL_URL"] = tunnel.url
        print(f"ComfyUI tunnel: {tunnel.url}")

    @modal.exit()
    def shutdown(self):
        """Clean up the tunnel."""
        if hasattr(self, "_tunnel_ctx"):
            self._tunnel_ctx.__exit__(None, None, None)

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
def prefetch(name: str = "default"):
    """Pre-initialize a ComfyUI installation into the volume.

    Runs the full init_installation (download, extract, clone, venv)
    on a cheap CPU container so the first GPU deploy only needs to
    checkout a branch and start ComfyUI (~1 min instead of ~7 min).
    """
    if COMFY_RUNNER_PATH not in sys.path:
        sys.path.insert(0, COMFY_RUNNER_PATH)

    from comfy_runner.config import get_installation
    from comfy_runner.installations import init_installation

    def _log(msg):
        print(msg, end="", flush=True)

    existing = get_installation(name)
    if existing:
        _log(f"Installation '{name}' already exists at {existing['path']}.\n")
        _log("Skipping prefetch (delete the installation first to re-init).\n")
        return

    _log(f"Initializing installation '{name}' (this may take a few minutes)...\n\n")
    init_installation(name=name, send_output=_log)

    _log("\nPrefetch complete. Installation ready in volume.\n")
    volume.commit()
