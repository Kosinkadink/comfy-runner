# comfy-runner

A Python toolkit for managing ComfyUI instances — installation, snapshot, process management, tunneling, and remote execution.

## Packages

- **`comfy_runner/`** — Core library (config, environment, process management, tunneling, shared paths, snapshots, nodes, git utilities, hosted providers)
- **`comfy_runner_cli/`** — CLI interface (`python -m comfy_runner_cli` or `python comfy_runner.py`)
- **`comfy_runner_server/`** — HTTP API server (`python -m comfy_runner_server`, Flask + Waitress)

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip.exe install -r requirements.txt
# Linux/macOS
.venv/bin/pip install -r requirements.txt
```

## GitHub Token

comfy-runner calls the GitHub API to fetch releases and manifests. Unauthenticated requests are rate-limited to 60/hour. To avoid hitting the limit, provide a token via either:

```bash
# Environment variable (recommended)
export GITHUB_TOKEN=ghp_...

# Or persist in comfy-runner config
python comfy_runner.py config set github_token ghp_...
```

The environment variable takes precedence over the config value.

## CLI Usage

```bash
python comfy_runner.py <command> [options]
python -m comfy_runner_cli <command> [options]
```

### Installation Management

```bash
# Create a new ComfyUI installation
comfy_runner.py init --name main --variant gpu --release latest --dir ./installs

# List all installations (alias: ls)
comfy_runner.py list

# Show detailed info about an installation (default: main)
comfy_runner.py info [name]

# Remove an installation
comfy_runner.py rm <name>
comfy_runner.py rm <name> --keep-files    # remove record but keep files on disk

# Set a config value on an installation
comfy_runner.py set <name> <key> <value>
comfy_runner.py set main launch_args "--cpu"

# View or set global configuration
comfy_runner.py config show
comfy_runner.py config set <key> <value>
comfy_runner.py config set shared_dir /path/to/shared
```

### Available Releases

```bash
# List available releases
comfy_runner.py releases
comfy_runner.py releases --variants       # show per-release variants
comfy_runner.py releases --limit 20       # default: 10
```

### Process Control

```bash
# Start an installation (default: main)
comfy_runner.py start [name]
comfy_runner.py start main --port 8188
comfy_runner.py start main --background
comfy_runner.py start main --port-conflict auto    # auto|fail
comfy_runner.py start main --extra-args "--cpu --lowvram"

# Stop a running installation
comfy_runner.py stop [name]

# Restart an installation
comfy_runner.py restart [name]
comfy_runner.py restart main --port 8189

# Show running state
comfy_runner.py status [name]

# Show logs from a running installation
comfy_runner.py logs [name]
```

### Custom Nodes

```bash
# List custom nodes for an installation
comfy_runner.py nodes list [name]

# Add a custom node (git URL or CNR node ID)
comfy_runner.py nodes add https://github.com/user/repo.git [name]
comfy_runner.py nodes add some-node-id [name] --version 1.0.0

# Remove a custom node
comfy_runner.py nodes rm <node_name> [name]

# Enable/disable a custom node
comfy_runner.py nodes enable <node_name> [name]
comfy_runner.py nodes disable <node_name> [name]
```

### Snapshots

```bash
# Capture snapshot from any ComfyUI directory
comfy_runner.py snapshot capture --path /path/to/comfyui --output snapshot.json --label "before update"

# Capture current state of a registered installation
comfy_runner.py snapshot save [name] --label "v1.0 stable"

# List snapshots
comfy_runner.py snapshot list [name]

# Show snapshot details
comfy_runner.py snapshot show <id> [name]

# Diff snapshot against current state
comfy_runner.py snapshot diff <id> [name]

# Restore to a snapshot
comfy_runner.py snapshot restore <id> [name]

# Export/import snapshots
comfy_runner.py snapshot export <id> [name] --output snapshot.json
comfy_runner.py snapshot import <file> [name]
```

### Deployment

```bash
# Deploy a PR, branch, tag, or commit
comfy_runner.py deploy [name] --pr 1234
comfy_runner.py deploy [name] --branch master
comfy_runner.py deploy [name] --tag v1.0.0
comfy_runner.py deploy [name] --commit abc1234
comfy_runner.py deploy [name] --reset    # reset to original release ref

# Update to the latest standalone release's ComfyUI ref
# (lightweight — only updates git checkout, not the standalone environment)
comfy_runner.py deploy [name] --latest

# Re-fetch the currently tracked branch or PR
# (requires a prior --branch or --pr deploy to set tracking)
comfy_runner.py deploy [name] --pull
```

Using `--branch` persists the branch name so `--pull` can re-fetch it later. The deploy command automatically stops the instance before deploying, installs changed requirements, restarts if it was running, and captures a post-update snapshot.

### Tunneling

Expose a running ComfyUI instance's port to the internet via ngrok or Tailscale Funnel.

```bash
# Start/stop a tunnel for an installation's ComfyUI port
comfy_runner.py tunnel start [name] --provider ngrok
comfy_runner.py tunnel start [name] --provider ngrok --domain myapp.ngrok-free.app
comfy_runner.py tunnel start [name] --provider tailscale
comfy_runner.py tunnel stop [name]

# View/set tunnel configuration
comfy_runner.py tunnel config --provider ngrok --authtoken <token>
comfy_runner.py tunnel config --region us
comfy_runner.py tunnel config --add-domain example.ngrok.io
comfy_runner.py tunnel config --rm-domain example.ngrok.io
```

These tunnels expose individual ComfyUI instances (their `--port`), not the runner server itself. For exposing the runner server, see **Tailscale Serve** below.

### Hosted GPU Deployments (RunPod)

Manage cloud GPU pods and network volumes via the RunPod REST API.

```bash
# Configure RunPod credentials (or set RUNPOD_API_KEY env var)
comfy_runner.py hosted config set runpod.api_key rk_...

# View hosted config (sensitive values are redacted in output)
comfy_runner.py hosted config show

# Set provider defaults
comfy_runner.py hosted config set runpod.default_gpu "NVIDIA A100 80GB"
comfy_runner.py hosted config set runpod.default_datacenter EU-RO-1

# Create a network volume
comfy_runner.py hosted volume create --name workspace --size 50 --region US-KS-2

# List configured volumes
comfy_runner.py hosted volume list

# Remove a volume (deletes from RunPod and local config)
comfy_runner.py hosted volume rm workspace

# Remove local config only, keep the volume on RunPod
comfy_runner.py hosted volume rm workspace --keep-remote

# One-shot: create volume + pod, ready to receive API commands
comfy_runner.py hosted init --name my-comfy --volume workspace
comfy_runner.py hosted init --name my-comfy --volume workspace --volume-size 100 --gpu "NVIDIA A100 80GB"

# Create a pod (lower-level, manual volume management)
comfy_runner.py hosted pod create --name my-comfy
comfy_runner.py hosted pod create --name my-comfy --gpu "NVIDIA A100 80GB" --volume workspace

# List all pods
comfy_runner.py hosted pod list

# Show pod details
comfy_runner.py hosted pod show <pod_id>

# Start / stop / terminate a pod
comfy_runner.py hosted pod start <pod_id>
comfy_runner.py hosted pod stop <pod_id>
comfy_runner.py hosted pod terminate <pod_id>    # permanent

# Get the proxy URL for a running pod (default port: 8188)
comfy_runner.py hosted pod url <pod_id>
comfy_runner.py hosted pod url <pod_id> --port 9189

# Deploy a PR/branch/tag to a hosted pod (polls until complete)
comfy_runner.py hosted deploy my-comfy --pr 1234
comfy_runner.py hosted deploy my-comfy --branch feature-x --start
comfy_runner.py hosted deploy my-comfy --reset

# Query system/hardware info from a hosted pod
comfy_runner.py hosted sysinfo my-comfy

# Check status of installations on a hosted pod
comfy_runner.py hosted status my-comfy

# Start/stop ComfyUI on a hosted pod
comfy_runner.py hosted start-comfy my-comfy
comfy_runner.py hosted stop-comfy my-comfy

# View ComfyUI logs from a hosted pod
comfy_runner.py hosted logs my-comfy
```

### How it works

The pod runs a thin Docker image (`ghcr.io/kosinkadink/comfy-runner:latest`) that clones comfy-runner from GitHub on boot and starts the comfy-runner HTTP server on port 9189. Everything else (init, deploy, start ComfyUI, etc.) is driven by API requests to that server — the same API used for local installations.

**Persistent cache:** When a network volume is mounted at `/workspace`, the startup script symlinks the download cache (`~/.comfy-runner/cache/`) to the volume. This avoids re-downloading the ~2.7GB standalone environment on each pod lifecycle. Installations and ComfyUI itself run on the fast container disk (NVMe) for best performance.

**CUDA compatibility:** The standalone environment bundles PyTorch built for CUDA 13.0, which requires NVIDIA driver ≥ 580. Many cloud hosts have older drivers. Pass `--cuda-compat` to `init` (or `"cuda_compat": true` in the deploy API) to auto-detect the host driver and reinstall PyTorch with a compatible CUDA version. Pod creation filters for hosts with CUDA ≥ 12.4 by default (override with `--cuda-versions`).

The hosted module lives under `comfy_runner/hosted/` and provides:

- **`config.py`** — Provider credentials, volume/pod registry, and API key fallback (`RUNPOD_API_KEY` env → config)
- **`runpod_api.py`** — Low-level RunPod REST API client (`https://rest.runpod.io/v1/`)
- **`runpod_provider.py`** — High-level `RunPodProvider` with sensible defaults for pod creation
- **`provider.py`** — `HostedProvider` protocol and shared dataclasses (`PodInfo`, `VolumeInfo`)
- **`remote.py`** — HTTP client for proxying commands to a remote comfy-runner server
- **`Dockerfile`** — Thin image definition for RunPod pods (no CUDA toolkit — PyTorch bundles its own)
- **`startup.sh`** — Pod entrypoint shim (clone comfy-runner, exec `startup_main.sh`)
- **`startup_main.sh`** — Main startup logic (install 7z, create venv, start server) — updated via git without image rebuild

### Tailscale Serve

Expose the runner server itself over your tailnet (private HTTPS, accessible only to devices on your Tailscale network).

```bash
# Expose runner server to tailnet
comfy_runner.py tailscale-serve start --port 9189

# Stop tailscale serve
comfy_runner.py tailscale-serve stop

# Show tailscale serve status
comfy_runner.py tailscale-serve status
```

## HTTP API Server

Start the server:

```bash
comfy_runner.py server --host 127.0.0.1 --port 9189
comfy_runner.py server --tailscale         # expose via tailscale serve (HTTPS over tailnet)
comfy_runner.py server --tunnels           # enable per-instance tunnel start/stop API endpoints
comfy_runner.py server --keep-instances    # keep instances running on shutdown
```

### Remote Access Setup

There are two ways to access the runner server remotely:

#### Tailscale (recommended for private access)

[Tailscale](https://tailscale.com/) creates a private mesh VPN between your devices. The `--tailscale` flag uses `tailscale serve` to expose the runner server over HTTPS on your tailnet.

1. Install Tailscale on both the server machine and your client machine
2. Start the server with `--tailscale`:
   ```bash
   comfy_runner.py server --tailscale --tunnels
   ```
3. The server prints a URL like `https://mybox.tailnet-name.ts.net:9189`
4. Add that URL as a runner server in pr-tracker config or use it directly

The server binds to `127.0.0.1` — Tailscale serve handles HTTPS termination and forwards to localhost. On shutdown, stale tailscale serve registrations are automatically cleaned up.

#### ngrok (for public access)

[ngrok](https://ngrok.com/) creates public HTTPS tunnels. Use it to expose individual ComfyUI instance ports (not the runner server itself).

1. Install ngrok and get an authtoken from [ngrok.com](https://dashboard.ngrok.com/)
2. Configure the authtoken:
   ```bash
   comfy_runner.py tunnel config --provider ngrok --authtoken 2abc...
   ```
3. Optionally configure reserved domains (otherwise you get random URLs):
   ```bash
   comfy_runner.py tunnel config --add-domain comfy-1.ngrok-free.app
   ```
4. Start a tunnel for a running installation:
   ```bash
   comfy_runner.py tunnel start myinstall --provider ngrok
   ```
5. Start the server with `--tunnels` to enable the tunnel start/stop API:
   ```bash
   comfy_runner.py server --tunnels
   ```

Multiple ngrok tunnels can run simultaneously — each gets a unique local API address. Domains from the pool are automatically allocated and released.

#### Combining Both

A typical remote setup uses Tailscale for the runner server and ngrok for individual ComfyUI instances:

```bash
comfy_runner.py server --tailscale --tunnels
```

This gives you private HTTPS access to the control API via Tailscale, with the ability to start public ngrok tunnels for ComfyUI instances via the API (`POST /<name>/tunnel/start`).

### Key Details

- Routes are prefixed with `/<name>/` for installation-specific operations.
- Long-running operations (deploy, restart, snapshot restore, node add/rm) run in background threads and return a `job_id`.
- Poll `GET /job/<job_id>` to check job status.
- All responses are JSON: `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/installations` | List all installations |
| `GET` | `/status` | Status of all installations |
| `GET` | `/system-info` | GPU, CPU, memory info |
| `GET` | `/config` | View global config (shared_dir, token status) |
| `PUT` | `/config` | Update global config (shared_dir, hf_token, modelscope_token) |
| `GET` | `/jobs` | List background jobs |
| `GET` | `/job/{id}` | Get job status and output |
| `POST` | `/job/{id}/cancel` | Cancel a running job |
| `GET` | `/{name}/status` | Installation status (running, port, health) |
| `GET` | `/{name}/info` | Detailed installation info |
| `POST` | `/{name}/start` | Start a stopped installation |
| `POST` | `/{name}/stop` | Stop a running installation |
| `POST` | `/{name}/restart` | Restart an installation |
| `POST` | `/{name}/deploy` | Deploy a branch, tag, PR, or commit |
| `PUT` | `/{name}/config` | Update installation config (launch_args) |
| `GET` | `/{name}/logs` | Read logs (with `?lines=N` or `?after=offset`) |
| `GET` | `/{name}/logs/sessions` | List log sessions |
| `GET` | `/{name}/nodes` | List custom nodes |
| `POST` | `/{name}/nodes` | Add/remove/enable/disable custom nodes |
| `GET/POST` | `/{name}/comfyui/{path}` | Proxy requests to running ComfyUI instance |
| `GET` | `/{name}/outputs` | List output files (`?prefix=`, `?limit=`, `?after=`) |
| `GET` | `/{name}/outputs/{file}` | Download an output file |
| `POST` | `/{name}/download-model` | Download a model by URL |
| `POST` | `/{name}/workflow-models` | Download models from a workflow |
| `GET` | `/{name}/snapshot` | List snapshots |
| `POST` | `/{name}/snapshot/save` | Save a snapshot |
| `POST` | `/{name}/snapshot/restore` | Restore a snapshot |
| `POST` | `/{name}/tunnel/start` | Start a tunnel |
| `POST` | `/{name}/tunnel/stop` | Stop a tunnel |

### OpenAPI Spec

The server auto-serves an OpenAPI 3.0.3 spec at `GET /openapi.json`. The spec is generated at runtime from route metadata in `comfy_runner_server/openapi.py` — no manual YAML to maintain. When routes change, update the `_ROUTES` list in that file and the spec updates automatically.

```bash
# Fetch the spec from a running server
curl http://localhost:9189/openapi.json | python -m json.tool
```

### Model Download Authentication

comfy-runner supports auth tokens for downloading models from private repositories:

```bash
# Set HuggingFace token
comfy_runner.py config set hf_token hf_abc123...

# Set ModelScope token
comfy_runner.py config set modelscope_token ms_abc123...

# View configured tokens (masked)
comfy_runner.py config show

# Or set via environment variables
export HF_TOKEN=hf_abc123...
export MODELSCOPE_SDK_TOKEN=ms_abc123...
```

Tokens are automatically used when downloading from `huggingface.co` or `modelscope.cn`/`modelscope.ai` URLs. Public models work without tokens.

## Testing

```bash
# Run all tests
.venv/bin/python -m pytest tests/ -q

# Run a specific test file
.venv/bin/python -m pytest tests/test_hosted_config.py -v
```

Tests use `pytest` with `unittest.mock.patch` for mocking. The `tmp_config_dir` fixture (see `tests/conftest.py`) redirects all config/cache paths to `tmp_path` so tests never touch the real home directory.

Test files follow the naming convention `tests/test_<module>.py` and are organized by class per feature area.

## Global Flags

| Flag | Description |
|------|-------------|
| `--json` | Machine-readable JSON output for all commands |
