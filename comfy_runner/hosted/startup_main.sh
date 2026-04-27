#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# comfy-runner pod startup – main logic.
#
# Exec'd by the bootstrap shim (startup.sh) after comfy-runner has
# been cloned/updated.  Changes here take effect on the next pod boot
# WITHOUT rebuilding the Docker image.
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# Log all output to a file for debugging
STARTUP_LOG="/tmp/comfy-runner-startup.log"
exec > >(tee -a "${STARTUP_LOG}") 2>&1

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

SERVER_TAILSCALE=""
if [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    log "Installing Tailscale..."
    if curl -fsSL https://tailscale.com/install.sh | sh; then
        TAILSCALE_STATE="/var/lib/tailscale"
        if [ -d "/workspace" ] && [ -w "/workspace" ]; then
            # Persist state across pod restarts via the network volume.
            # tailscaled --state expects a FILE path, not a directory.
            mkdir -p "/workspace/.tailscale"
            TAILSCALE_STATE="/workspace/.tailscale/tailscaled.state"
        fi
        mkdir -p /var/run/tailscale
        # Use userspace networking — RunPod containers lack /dev/net/tun
        tailscaled --state="${TAILSCALE_STATE}" --socket=/var/run/tailscale/tailscaled.sock --tun=userspace-networking &
        # Wait for tailscaled socket to become available (up to 10s)
        for i in $(seq 1 20); do
            [ -S /var/run/tailscale/tailscaled.sock ] && break
            sleep 0.5
        done

        TS_HOSTNAME="${TAILSCALE_HOSTNAME:-comfy-runner}"
        TS_TAGS="${TAILSCALE_TAGS:-tag:runpod}"

        # ── Remove stale Tailscale devices via the Tailscale REST API.
        # When a pod with the same name was previously connected, its
        # device record persists in the tailnet. Joining without cleanup
        # causes Tailscale to assign a suffixed hostname (e.g.
        # ``comfy-pool-image-1``), breaking URL resolution. Delete any
        # device whose hostname matches our target (or target-N) before
        # ``tailscale up``.
        if [ -n "${TAILSCALE_API_KEY:-}" ] && [ -n "${TAILSCALE_TAILNET:-}" ]; then
            log "Cleaning up stale Tailscale devices for hostname '${TS_HOSTNAME}'..."
            TS_HOSTNAME="${TS_HOSTNAME}" \
            TAILSCALE_API_KEY="${TAILSCALE_API_KEY}" \
            TAILSCALE_TAILNET="${TAILSCALE_TAILNET}" \
            python3 - <<'PYEOF' || log "WARNING: stale device cleanup failed (continuing)"
import json
import os
import re
import sys
import urllib.error
import urllib.request

target = os.environ["TS_HOSTNAME"]
api_key = os.environ["TAILSCALE_API_KEY"]
tailnet = os.environ["TAILSCALE_TAILNET"]

# Match the exact hostname or hostname-N (Tailscale's auto-suffix pattern)
suffix_re = re.compile(r"^" + re.escape(target) + r"(-\d+)?$")

base = f"https://api.tailscale.com/api/v2/tailnet/{tailnet}"
list_req = urllib.request.Request(
    f"{base}/devices",
    headers={"Authorization": f"Bearer {api_key}"},
)
try:
    with urllib.request.urlopen(list_req, timeout=15) as resp:
        data = json.load(resp)
except urllib.error.HTTPError as e:
    print(f"[ts-cleanup] list devices failed: HTTP {e.code} {e.reason}")
    sys.exit(1)
except Exception as e:
    print(f"[ts-cleanup] list devices failed: {e}")
    sys.exit(1)

devices = data.get("devices", []) or []
matches = []
for d in devices:
    host = d.get("hostname", "") or ""
    # Tailscale also exposes ``name`` as FQDN; first DNS label is the host
    if not host:
        fqdn = d.get("name", "") or ""
        host = fqdn.split(".", 1)[0]
    if suffix_re.match(host):
        matches.append((d.get("id", ""), host))

if not matches:
    print(f"[ts-cleanup] no stale devices for '{target}'")
    sys.exit(0)

for did, host in matches:
    if not did:
        continue
    del_req = urllib.request.Request(
        f"https://api.tailscale.com/api/v2/device/{did}",
        method="DELETE",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(del_req, timeout=15) as r:
            r.read()
        print(f"[ts-cleanup] deleted device id={did} hostname={host}")
    except urllib.error.HTTPError as e:
        print(f"[ts-cleanup] delete id={did} ({host}) failed: HTTP {e.code} {e.reason}")
    except Exception as e:
        print(f"[ts-cleanup] delete id={did} ({host}) failed: {e}")
PYEOF
        else
            log "TAILSCALE_API_KEY/TAILSCALE_TAILNET not set — skipping stale device cleanup"
        fi

        if timeout 30 tailscale up --auth-key="${TAILSCALE_AUTH_KEY}" --hostname="${TS_HOSTNAME}" --ssh --advertise-tags="${TS_TAGS}" --force-reauth 2>&1; then
            log "Tailscale up: $(tailscale ip -4 2>/dev/null || echo 'unknown')"
            # Don't pass --tailscale to the server — pods serve plain HTTP.
            # Tailscale provides the encrypted tunnel; tailscale serve (HTTPS)
            # would break the RunPod proxy and http:// URLs used by the central server.

            # ── Verify the actually-assigned Tailscale hostname.
            # Even with --hostname=X, Tailscale may suffix the name
            # if a stale device with that hostname still exists.
            sleep 1
            TS_STATUS_JSON="$(timeout 10 tailscale status --json 2>/dev/null || echo '{}')"
            ACTUAL_HOST="$(echo "${TS_STATUS_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("Self") or {}).get("HostName",""))' 2>/dev/null || echo "")"
            ACTUAL_DNS="$(echo "${TS_STATUS_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("Self") or {}).get("DNSName",""))' 2>/dev/null || echo "")"
            log "Tailscale hostname: expected='${TS_HOSTNAME}' actual='${ACTUAL_HOST:-unknown}' dns='${ACTUAL_DNS:-unknown}'"
            if [ -n "${ACTUAL_HOST}" ] && [ "${ACTUAL_HOST}" != "${TS_HOSTNAME}" ]; then
                log "WARNING: Tailscale hostname drift detected — server URL resolution may rely on the API fallback"
            fi
        else
            TS_EXIT=$?
            log "WARNING: tailscale up failed (exit ${TS_EXIT}) — continuing without Tailscale"
            # Dump tailscaled logs for debugging
            tailscale bugreport 2>/dev/null || true
        fi
    else
        log "WARNING: Tailscale install failed — continuing without Tailscale"
    fi
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
    --listen "${SERVER_HOST}" \
    --port "${SERVER_PORT}" \
    ${SERVER_TAILSCALE}
