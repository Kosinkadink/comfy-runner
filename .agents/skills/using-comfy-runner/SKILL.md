---
name: using-comfy-runner
description: "Manages ComfyUI instances via comfy-runner: install, start/stop, deploy PRs/branches, snapshots, custom nodes, tunneling, model downloads, hosted GPU pods (RunPod), and remote server API. Use when asked to deploy ComfyUI, manage installations, expose via Tailscale/ngrok, or interact with a remote comfy-runner server."
---

# Using comfy-runner

comfy-runner is a Python toolkit for managing ComfyUI instances — installation, snapshot, process management, tunneling, and remote execution. See `comfy-runner/README.md` for full details.

## Setup

```powershell
# Activate venv (Windows)
comfy-runner\.venv\Scripts\python.exe comfy-runner\comfy_runner.py <command>

# Or use the module form
comfy-runner\.venv\Scripts\python.exe -m comfy_runner_cli <command>
```

If `.venv` doesn't exist, run `comfy-runner\setup_env.ps1` (Windows) or `comfy-runner/setup_env.sh` (Linux/macOS) first.

Set `GITHUB_TOKEN` for GitHub API access (releases, manifests):

```powershell
$env:GITHUB_TOKEN = (Get-Content githubtoken.txt -Raw).Trim(); comfy-runner\.venv\Scripts\python.exe comfy-runner\comfy_runner.py <command>; Remove-Item Env:\GITHUB_TOKEN
```

## Tailscale Resolution

Users often provide bare device names (e.g., "deploy to mybox") or short hostnames instead of full URLs. comfy-runner servers exposed via Tailscale **require the full MagicDNS FQDN** — short names cause TLS handshake failures.

### When given a device name or partial URL

1. **Check if Tailscale is available:**
   ```powershell
   tailscale status --json
   ```
   If this fails, Tailscale is not installed or not running.

2. **Get the MagicDNS suffix:**
   ```powershell
   tailscale status --json | python -c "import json,sys; print(json.load(sys.stdin)['MagicDNSSuffix'])"
   ```
   This returns something like `tailnet-name.ts.net`.

3. **Construct the full FQDN URL:**
   ```
   https://{device_name}.{magic_dns_suffix}:9189
   ```
   Example: device `mybox` + suffix `tail1234.ts.net` → `https://mybox.tail1234.ts.net:9189`

4. **Validate the device exists on the tailnet:**
   ```powershell
   tailscale status
   ```
   Lists all devices — confirm the target device name appears before attempting to connect.

### Common mistakes to avoid

- **Never use `http://`** — Tailscale serve enforces HTTPS.
- **Never use short hostnames** like `https://mybox:9189` — TLS cert is issued for the FQDN.
- **Default runner server port is 9189** unless configured otherwise.

## CLI Quick Reference

### Installation Management

| Command | Description |
|---------|-------------|
| `init --name main --variant gpu --release latest --dir ./installs` | Create new installation |
| `list` | List all installations |
| `info [name]` | Show installation details |
| `rm <name>` | Remove an installation |
| `set <name> <key> <value>` | Set config on an installation |
| `config show` | View global config |
| `config set <key> <value>` | Set global config value |
| `releases` | List available releases |

### Process Control

| Command | Description |
|---------|-------------|
| `start [name]` | Start an installation |
| `start [name] --port 8188 --background` | Start on specific port, in background |
| `stop [name]` | Stop a running installation |
| `restart [name]` | Restart an installation |
| `status [name]` | Show running state |
| `logs [name]` | Show logs |

### Deployment

| Command | Description |
|---------|-------------|
| `deploy [name] --pr 1234` | Deploy a PR |
| `deploy [name] --branch feature-x` | Deploy a branch (persists for `--pull`) |
| `deploy [name] --tag v1.0.0` | Deploy a tag |
| `deploy [name] --commit abc1234` | Deploy a specific commit |
| `deploy [name] --latest` | Update to latest release's ComfyUI ref |
| `deploy [name] --pull` | Re-fetch currently tracked branch/PR |
| `deploy [name] --reset` | Reset to original release ref |

Deploy auto-stops the instance, installs changed requirements, restarts if it was running, and captures a snapshot.

### Custom Nodes

| Command | Description |
|---------|-------------|
| `nodes list [name]` | List custom nodes |
| `nodes add <url_or_id> [name]` | Add a custom node |
| `nodes rm <node_name> [name]` | Remove a custom node |
| `nodes enable/disable <node_name> [name]` | Enable/disable a node |

### Snapshots

| Command | Description |
|---------|-------------|
| `snapshot save [name] --label "description"` | Save current state |
| `snapshot list [name]` | List snapshots |
| `snapshot diff <id> [name]` | Diff against current state |
| `snapshot restore <id> [name]` | Restore to a snapshot |
| `snapshot export/import` | Export/import snapshot files |
| `snapshot capture --path /path --output file.json` | Capture from any ComfyUI dir |

### Tunneling

| Command | Description |
|---------|-------------|
| `tunnel start [name] --provider ngrok` | Expose ComfyUI port via ngrok |
| `tunnel start [name] --provider tailscale` | Expose ComfyUI port via Tailscale funnel |
| `tunnel stop [name]` | Stop a tunnel |
| `tunnel config --provider ngrok --authtoken <tok>` | Configure ngrok auth |

Tunnels expose individual ComfyUI instance ports, not the runner server.

### Tailscale Serve (Runner Server)

| Command | Description |
|---------|-------------|
| `tailscale-serve start --port 9189` | Expose runner server over tailnet |
| `tailscale-serve stop` | Stop tailscale serve |
| `tailscale-serve status` | Show serve status |

### Model Downloads

| Command | Description |
|---------|-------------|
| `download-model --url <url> --dir checkpoints [name]` | Download a model |
| `remote upload-model --server <url> --file <f> --dir <d> [name]` | Upload model to remote server |

Supports `HF_TOKEN` and `MODELSCOPE_TOKEN` env vars for private models.

### Hosted GPU (RunPod)

| Command | Description |
|---------|-------------|
| `hosted config set runpod.api_key rk_...` | Set RunPod API key |
| `hosted init --name my-comfy --volume workspace` | Create volume + pod |
| `hosted pod list` | List pods |
| `hosted pod start/stop/terminate <pod_id>` | Manage pod lifecycle |
| `hosted deploy <name> --pr 1234` | Deploy to a hosted pod |
| `hosted start-comfy/stop-comfy <name>` | Start/stop ComfyUI on pod |
| `hosted logs <name>` | View ComfyUI logs from pod |
| `hosted sysinfo <name>` | Query hardware info |
| `hosted volume create --name ws --size 50 --region US-KS-2` | Create network volume |

Pass `--cuda-compat` to `hosted init` when the cloud host has older NVIDIA drivers.

### HTTP Server

```powershell
comfy_runner.py server --host 127.0.0.1 --port 9189
comfy_runner.py server --tailscale       # expose via tailscale serve
comfy_runner.py server --tunnels         # enable tunnel API
```

## HTTP API Summary

All responses are JSON: `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.

Routes use `/<name>/` prefix for installation-specific operations. Long-running operations return a `job_id` — poll `GET /job/<job_id>` for status.

**For the current list of endpoints, read the "API Endpoints" section in `comfy-runner/README.md`.** Do not rely on a hardcoded list — the README is the source of truth and may have newer endpoints.

If a server is running, fetch `GET /openapi.json` for the complete auto-generated OpenAPI 3.0.3 spec with full parameter details.

## Key Gotchas

1. **`launch_args` replaces, doesn't merge.** `POST /{name}/deploy` with `launch_args` overwrites the existing value. Always read current args via `GET /{name}/info` first, then include all desired flags.

2. **Tailscale FQDN required.** Always use the full MagicDNS FQDN (e.g., `https://mybox.tailnet-name.ts.net:9189`). See the Tailscale Resolution section above.

3. **`--cuda-compat` for cloud GPUs.** The standalone environment bundles PyTorch for CUDA 13.0 (driver ≥ 580). Use `--cuda-compat` on `init`/`hosted init` to auto-detect and reinstall a compatible PyTorch.

4. **`--json` flag.** All CLI commands support `--json` for machine-readable output.

5. **OpenAPI spec.** When unsure about API parameters, fetch `/openapi.json` from the running server.
