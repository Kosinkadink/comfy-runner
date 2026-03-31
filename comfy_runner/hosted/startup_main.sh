#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# comfy-runner pod startup – main logic.
#
# Exec'd by the bootstrap shim (startup.sh) after comfy-runner has
# been cloned/updated.  Changes here take effect on the next pod boot
# WITHOUT rebuilding the Docker image.
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

INSTALL_DIR="/opt/comfy-runner"
VENV_DIR="${INSTALL_DIR}/.venv"
SERVER_HOST="0.0.0.0"
SERVER_PORT="${COMFY_RUNNER_PORT:-9189}"

log() { echo "[comfy-runner] $(date '+%H:%M:%S') $*"; }

# ── 1. Ensure native 7z is available (fast extraction) ───────────────

if ! command -v 7z &>/dev/null; then
    log "Installing p7zip-full for native 7z extraction..."
    apt-get update -qq && apt-get install -y -qq p7zip-full && rm -rf /var/lib/apt/lists/*
fi

# ── 2. Create venv and install requirements ──────────────────────────

cd "${INSTALL_DIR}"

if [ ! -d "${VENV_DIR}" ]; then
    log "Creating venv..."
    python3 -m venv "${VENV_DIR}"
fi

log "Installing requirements..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r requirements.txt

# ── 3. Start comfy-runner server ─────────────────────────────────────

log "Starting comfy-runner server on ${SERVER_HOST}:${SERVER_PORT}..."
exec "${VENV_DIR}/bin/python" -m comfy_runner_server \
    --host "${SERVER_HOST}" \
    --port "${SERVER_PORT}"
