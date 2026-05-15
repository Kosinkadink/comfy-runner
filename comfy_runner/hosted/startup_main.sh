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

# ── 0. Persist comfy-runner state on volume (if mounted) ─────────────
# RunPod stop/start wipes the entire container rootfs and reschedules
# the pod onto a fresh host, so anything in $HOME (installations,
# config.json, port-locks, downloaded model cache) is lost on resume.
# Only /workspace survives — RunPod auto-allocates an ephemeral volume
# there whenever ``volumeMountPath`` is set on the pod, which the
# provider does unconditionally.
#
# Point COMFY_RUNNER_HOME at /workspace/.comfy-runner so that EVERY
# subsystem (config, installations dir, cache, bin, port-locks, tunnel
# state, tailscale-serves) lives on the persistent volume. Single
# env var, no symlink dance — comfy_runner.config reads
# COMFY_RUNNER_HOME on import and every other module derives off the
# resulting CONFIG_DIR.

if [ -d "/workspace" ] && [ -w "/workspace" ]; then
    export COMFY_RUNNER_HOME="/workspace/.comfy-runner"
    mkdir -p "${COMFY_RUNNER_HOME}"
    log "comfy-runner state on volume: COMFY_RUNNER_HOME=${COMFY_RUNNER_HOME}"
fi

# ── 1. Ensure native 7z is available (fast extraction) ───────────────

if ! command -v 7z &>/dev/null; then
    log "Installing p7zip-full for native 7z extraction..."
    apt-get update -qq && apt-get install -y -qq p7zip-full && rm -rf /var/lib/apt/lists/*
fi

# ── 2. Tailscale auto-join ────────────────────────────────────────────
#
# Two auth flows are supported (in order of preference):
#
#   1. OAuth client (recommended) — TAILSCALE_OAUTH_CLIENT_ID +
#      TAILSCALE_OAUTH_CLIENT_SECRET in env. We exchange them for a
#      bearer token and mint a fresh single-use ephemeral auth key per
#      boot. This works reliably across arbitrary pod stop/start
#      cycles because we never reuse a key. See
#      https://tailscale.com/kb/1215/oauth-clients .
#
#   2. Static auth key (legacy) — TAILSCALE_AUTH_KEY in env. Only
#      reliable if the key is configured as Reusable; single-use keys
#      will fail on the second boot. Kept as a fallback for backwards
#      compatibility with single-developer setups.
#
# This block is the "front door" to the tailnet. If we don't get on
# the tailnet, the pod is unreachable from the central server and
# every later step is wasted compute. We therefore exit non-zero on
# any failure here so RunPod surfaces a crashed container instead of
# leaving an unreachable RUNNING zombie.

SERVER_TAILSCALE=""
TS_HOSTNAME="${TAILSCALE_HOSTNAME:-comfy-runner}"
TS_TAGS="${TAILSCALE_TAGS:-tag:runpod}"

# Decide which flow to use up front.
TS_FLOW=""
if [ -n "${TAILSCALE_OAUTH_CLIENT_ID:-}" ] && [ -n "${TAILSCALE_OAUTH_CLIENT_SECRET:-}" ]; then
    TS_FLOW="oauth"
elif [ -n "${TAILSCALE_AUTH_KEY:-}" ]; then
    TS_FLOW="static"
fi

if [ -n "${TS_FLOW}" ]; then
    log "Tailscale auth flow: ${TS_FLOW}"
    log "Installing Tailscale..."
    if ! curl -fsSL https://tailscale.com/install.sh | sh; then
        log "ERROR: Tailscale install failed — pod cannot join tailnet, aborting boot"
        exit 1
    fi

    # ── State location.
    # CI runner pods don't mount /workspace, so state lives on the
    # container disk at /var/lib/tailscale (which persists across
    # stop/start). However, persisted state can carry a stale node
    # identity that conflicts with re-auth, especially after the
    # corresponding tailnet device record was GC'd. Wipe it on every
    # boot so we always register fresh — combined with ephemeral keys,
    # this gives us a clean tailnet device per boot with no leftover
    # state to fight.
    TAILSCALE_STATE="/var/lib/tailscale"
    if [ -d "/workspace" ] && [ -w "/workspace" ]; then
        # tailscaled --state expects a FILE path, not a directory.
        mkdir -p "/workspace/.tailscale"
        TAILSCALE_STATE="/workspace/.tailscale/tailscaled.state"
    fi
    if [ -d "${TAILSCALE_STATE}" ]; then
        log "Wiping persisted tailscaled state directory: ${TAILSCALE_STATE}"
        rm -rf "${TAILSCALE_STATE}"/*
    elif [ -f "${TAILSCALE_STATE}" ]; then
        log "Wiping persisted tailscaled state file: ${TAILSCALE_STATE}"
        rm -f "${TAILSCALE_STATE}"
    fi

    mkdir -p /var/run/tailscale
    # Use userspace networking — RunPod containers lack /dev/net/tun
    tailscaled --state="${TAILSCALE_STATE}" --socket=/var/run/tailscale/tailscaled.sock --tun=userspace-networking &
    # Wait for tailscaled socket to become available (up to 10s)
    for i in $(seq 1 20); do
        [ -S /var/run/tailscale/tailscaled.sock ] && break
        sleep 0.5
    done
    if [ ! -S /var/run/tailscale/tailscaled.sock ]; then
        log "ERROR: tailscaled socket did not appear within 10s — aborting boot"
        exit 1
    fi

    # ── Acquire a fresh auth key.
    TS_AUTH_KEY=""
    if [ "${TS_FLOW}" = "oauth" ]; then
        log "Minting fresh ephemeral auth key via Tailscale OAuth..."
        TS_AUTH_KEY="$(
            TAILSCALE_OAUTH_CLIENT_ID="${TAILSCALE_OAUTH_CLIENT_ID}" \
            TAILSCALE_OAUTH_CLIENT_SECRET="${TAILSCALE_OAUTH_CLIENT_SECRET}" \
            TS_TAGS="${TS_TAGS}" \
            python3 - <<'PYEOF'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

client_id = os.environ["TAILSCALE_OAUTH_CLIENT_ID"]
client_secret = os.environ["TAILSCALE_OAUTH_CLIENT_SECRET"]
tags = [t.strip() for t in os.environ["TS_TAGS"].split(",") if t.strip()]

# Step 1: OAuth client_credentials -> bearer token
token_req = urllib.request.Request(
    "https://api.tailscale.com/api/v2/oauth/token",
    data=urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("ascii"),
    method="POST",
)
try:
    with urllib.request.urlopen(token_req, timeout=20) as resp:
        token_data = json.load(resp)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")[:300]
    print(f"[ts-mint] OAuth token exchange failed: HTTP {e.code} {e.reason}: {body}",
          file=sys.stderr)
    sys.exit(2)
except Exception as e:
    print(f"[ts-mint] OAuth token exchange failed: {e}", file=sys.stderr)
    sys.exit(2)
bearer = token_data.get("access_token")
if not bearer:
    print(f"[ts-mint] OAuth response missing access_token: {token_data!r}",
          file=sys.stderr)
    sys.exit(2)

# Step 2: mint a single-use ephemeral pre-authorized key on the
# tailnet the OAuth client belongs to ("-" is the magic alias).
key_req = urllib.request.Request(
    "https://api.tailscale.com/api/v2/tailnet/-/keys",
    data=json.dumps({
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": True,
                    "preauthorized": True,
                    "tags": tags,
                },
            },
        },
        # 10 min — plenty of time for tailscale up to consume it.
        "expirySeconds": 600,
    }).encode("utf-8"),
    method="POST",
    headers={
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    },
)
try:
    with urllib.request.urlopen(key_req, timeout=20) as resp:
        key_data = json.load(resp)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")[:300]
    print(f"[ts-mint] auth key creation failed: HTTP {e.code} {e.reason}: {body}",
          file=sys.stderr)
    sys.exit(3)
except Exception as e:
    print(f"[ts-mint] auth key creation failed: {e}", file=sys.stderr)
    sys.exit(3)
key = key_data.get("key")
if not key:
    print(f"[ts-mint] auth key response missing 'key': {key_data!r}",
          file=sys.stderr)
    sys.exit(3)
# Print only the key on stdout; diagnostics go to stderr.
print(key)
PYEOF
        )"
        if [ -z "${TS_AUTH_KEY}" ]; then
            log "ERROR: failed to mint Tailscale auth key via OAuth — aborting boot"
            exit 1
        fi
        log "Minted ephemeral auth key (single-use, ${TS_TAGS})"
    else
        TS_AUTH_KEY="${TAILSCALE_AUTH_KEY}"
    fi

    # ── Join the tailnet. Bumped from 30s to 60s to ride out
    # transient API hiccups on cold-booting hosts.
    if ! timeout 60 tailscale up --auth-key="${TS_AUTH_KEY}" --hostname="${TS_HOSTNAME}" --ssh --advertise-tags="${TS_TAGS}" --force-reauth 2>&1; then
        TS_EXIT=$?
        log "ERROR: tailscale up failed (exit ${TS_EXIT}) — pod cannot join tailnet, aborting boot"
        # Dump tailscaled logs for debugging
        tailscale bugreport 2>/dev/null || true
        exit 1
    fi
    log "Tailscale up: $(tailscale ip -4 2>/dev/null || echo 'unknown')"
    # Don't pass --tailscale to the server — pods serve plain HTTP.
    # Tailscale provides the encrypted tunnel; tailscale serve (HTTPS)
    # would break the RunPod proxy and http:// URLs used by the central server.

    # ── Verify the actually-assigned Tailscale hostname. With ephemeral
    # keys + state-wipe each boot, suffix collisions should not happen,
    # but we still log for diagnostic visibility.
    sleep 1
    TS_STATUS_JSON="$(timeout 10 tailscale status --json 2>/dev/null || echo '{}')"
    ACTUAL_HOST="$(echo "${TS_STATUS_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("Self") or {}).get("HostName",""))' 2>/dev/null || echo "")"
    ACTUAL_DNS="$(echo "${TS_STATUS_JSON}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("Self") or {}).get("DNSName",""))' 2>/dev/null || echo "")"
    log "Tailscale hostname: expected='${TS_HOSTNAME}' actual='${ACTUAL_HOST:-unknown}' dns='${ACTUAL_DNS:-unknown}'"
    if [ -n "${ACTUAL_HOST}" ] && [ "${ACTUAL_HOST}" != "${TS_HOSTNAME}" ]; then
        log "WARNING: Tailscale hostname drift detected — server URL resolution may rely on the API fallback"
    fi
else
    log "No Tailscale credentials in env — skipping tailnet join"
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
