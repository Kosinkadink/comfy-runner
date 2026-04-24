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

# ── 0. Persist download cache on volume (if mounted) ─────────────────
# Installations run on fast container disk (~/.comfy-runner/installations/).
# Only the download cache (large .7z archives) is symlinked to the volume
# so re-downloads are avoided across pod stop/restart/terminate cycles.

if [ -d "/workspace" ] && [ -w "/workspace" ]; then
    CACHE_ON_VOLUME="/workspace/.comfy-runner/cache"
    LOCAL_CACHE="${HOME}/.comfy-runner/cache"
    mkdir -p "${CACHE_ON_VOLUME}"
    mkdir -p "$(dirname "${LOCAL_CACHE}")"
    if [ ! -L "${LOCAL_CACHE}" ]; then
        rm -rf "${LOCAL_CACHE}"
        ln -s "${CACHE_ON_VOLUME}" "${LOCAL_CACHE}"
        log "Download cache linked to volume: ${LOCAL_CACHE} → ${CACHE_ON_VOLUME}"
    fi
fi

# ── 1. Ensure native 7z is available (fast extraction) ───────────────

if ! command -v 7z &>/dev/null; then
    log "Installing p7zip-full for native 7z extraction..."
    apt-get update -qq && apt-get install -y -qq p7zip-full && rm -rf /var/lib/apt/lists/*
fi

# ── 2. Tailscale auto-join ────────────────────────────────────────────

if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    log "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sh

    TAILSCALE_STATE="/var/lib/tailscale"
    if [ -d "/workspace" ] && [ -w "/workspace" ]; then
        TAILSCALE_STATE="/workspace/.tailscale"
        mkdir -p "${TAILSCALE_STATE}"
    fi
    tailscaled --state="${TAILSCALE_STATE}" --socket=/var/run/tailscale/tailscaled.sock &
    sleep 2

    TS_HOSTNAME="${TAILSCALE_HOSTNAME:-comfy-runner}"
    tailscale up --auth-key="${TAILSCALE_AUTH_KEY}" --hostname="${TS_HOSTNAME}"
    log "Tailscale up: $(tailscale ip -4 2>/dev/null || echo 'unknown')"
    SERVER_TAILSCALE="--tailscale"
else
    SERVER_TAILSCALE=""
fi

# ── 3. Create venv and install requirements ──────────────────────────

cd "${INSTALL_DIR}"

if [ ! -d "${VENV_DIR}" ]; then
    log "Creating venv..."
    python3 -m venv "${VENV_DIR}"
fi

log "Installing requirements..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r requirements.txt

# ── 4. Start comfy-runner server ─────────────────────────────────────

log "Starting comfy-runner server on ${SERVER_HOST}:${SERVER_PORT}..."
exec "${VENV_DIR}/bin/python" -m comfy_runner_server \
    --host "${SERVER_HOST}" \
    --port "${SERVER_PORT}" \
    ${SERVER_TAILSCALE}
