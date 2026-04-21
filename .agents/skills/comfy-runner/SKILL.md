---
name: comfy-runner
description: "Manages ComfyUI instances via comfy-runner CLI and HTTP API — installation, deployment, snapshots, tunneling, hosted GPU pods. Use when asked to deploy PRs, manage ComfyUI installations, start/stop instances, create snapshots, or interact with remote comfy-runner servers."
---

# comfy-runner

A Python toolkit for managing ComfyUI instances — installation, snapshot, process management, tunneling, and remote execution.

## Packages

- **`comfy_runner/`** — Core library
- **`comfy_runner_cli/`** — CLI interface
- **`comfy_runner_server/`** — HTTP API server (Flask + Waitress)

## Running the CLI

```bash
# From the comfy-runner directory:
.venv\Scripts\python.exe comfy_runner.py <command> [options]
```

## Key CLI Commands

| Command | Description |
|---------|-------------|
| `init --name <n> --variant gpu --release latest` | Create installation |
| `list` | List installations |
| `info [name]` | Show installation details |
| `start [name] --port 8188` | Start ComfyUI |
| `stop [name]` | Stop ComfyUI |
| `deploy [name] --pr 1234` | Deploy a PR |
| `deploy [name] --branch feature-x` | Deploy a branch |
| `deploy [name] --latest` | Update to latest release |
| `deploy [name] --pull` | Re-fetch tracked branch/PR |
| `snapshot save [name] --label "..."` | Save snapshot |
| `snapshot restore <id> [name]` | Restore snapshot |
| `nodes list/add/rm/enable/disable` | Manage custom nodes |
| `server --tailscale --tunnels` | Start HTTP API server |

## HTTP API

The server runs on port 9189. All responses are JSON (`{"ok": true, ...}`).

- Routes are prefixed with `/<name>/` for installation-specific operations
- Long-running operations return a `job_id` — poll `GET /job/<job_id>` for status
- The server auto-serves an OpenAPI spec at `GET /openapi.json` — **fetch it to discover all endpoints and parameters**

### Important API Caveat

`POST /<name>/deploy` accepts a `launch_args` field that **replaces** the existing value (does not merge). Always read current args via `GET /<name>/info` first to avoid dropping flags like `--cuda-device`, `--enable-manager`, etc.

## Remote Access

- **Tailscale** (private): `comfy_runner.py server --tailscale` — exposes via `https://<hostname>.<tailnet>.ts.net:9189`
- **Must use full MagicDNS FQDN** — short hostnames cause TLS failures
- **ngrok** (public): For individual ComfyUI instance ports, not the runner server itself

## Hosted GPU (RunPod)

```bash
hosted config set runpod.api_key rk_...
hosted init --name my-comfy --volume workspace
hosted deploy my-comfy --pr 1234
hosted pod start/stop/terminate <pod_id>
```

## When Adding/Changing Server Endpoints

Always update `comfy_runner_server/openapi.py` — add or update the `_ROUTES` list entry.

## Testing

```bash
.venv\Scripts\python.exe -m pytest tests/ -q
```

## Reference

Read `comfy-runner/README.md` for complete CLI usage, all API endpoints, and setup instructions.
Read `comfy-runner/AGENTS.md` for agent-specific guidelines.
