#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# comfy-runner pod startup – thin bootstrap shim.
#
# This script is baked into the Docker image.  It clones (or updates)
# comfy-runner from GitHub, then re-execs the *cloned* copy of
# startup_main.sh so that any startup improvements land without
# rebuilding the image.
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_BASE="${COMFY_RUNNER_REPO:-https://github.com/Kosinkadink/comfy-runner.git}"
REPO_BRANCH="${COMFY_RUNNER_BRANCH:-main}"

# If GITHUB_TOKEN is set, inject it into the clone URL for private repos
if [ -n "${GITHUB_TOKEN:-}" ]; then
    REPO_URL=$(echo "${REPO_BASE}" | sed "s|https://|https://${GITHUB_TOKEN}@|")
else
    REPO_URL="${REPO_BASE}"
fi
INSTALL_DIR="/opt/comfy-runner"

log() { echo "[comfy-runner] $(date '+%H:%M:%S') $*"; }

# ── Clone or update comfy-runner ─────────────────────────────────────

if [ -d "${INSTALL_DIR}/.git" ]; then
    log "Updating comfy-runner (branch: ${REPO_BRANCH})..."
    cd "${INSTALL_DIR}"
    git fetch --all --quiet
    git checkout "${REPO_BRANCH}" --quiet
    git reset --hard "origin/${REPO_BRANCH}" --quiet
else
    log "Cloning comfy-runner (branch: ${REPO_BRANCH})..."
    git clone --branch "${REPO_BRANCH}" --single-branch "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"
log "comfy-runner at $(git rev-parse --short HEAD)"

# ── Hand off to the cloned startup_main.sh ───────────────────────────
# This allows startup logic to evolve without image rebuilds.

MAIN_SCRIPT="${INSTALL_DIR}/comfy_runner/hosted/startup_main.sh"
if [ -f "${MAIN_SCRIPT}" ]; then
    exec bash "${MAIN_SCRIPT}"
else
    # Fallback for older commits that don't have startup_main.sh yet:
    # run the original inline logic.
    log "startup_main.sh not found — running inline fallback"

    VENV_DIR="${INSTALL_DIR}/.venv"
    SERVER_HOST="0.0.0.0"
    SERVER_PORT="${COMFY_RUNNER_PORT:-9189}"

    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating venv..."
        python3 -m venv "${VENV_DIR}"
    fi
    log "Installing requirements..."
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r requirements.txt

    log "Starting comfy-runner server on ${SERVER_HOST}:${SERVER_PORT}..."
    exec "${VENV_DIR}/bin/python" -m comfy_runner_server \
        --host "${SERVER_HOST}" \
        --port "${SERVER_PORT}"
fi
