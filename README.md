# comfy-runner

A Python toolkit for managing ComfyUI instances — installation, snapshot, process management, tunneling, and remote execution.

## Packages

- **`comfy_runner/`** — Core library (config, environment, process management, tunneling, shared paths, snapshots, nodes, git utilities)
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
comfy_runner.py deploy [name] --branch feature-x
comfy_runner.py deploy [name] --tag v1.0.0
comfy_runner.py deploy [name] --commit abc1234
comfy_runner.py deploy [name] --reset    # reset to default state
```

### Tunneling

```bash
# Start/stop a tunnel
comfy_runner.py tunnel start [name] --provider ngrok
comfy_runner.py tunnel start [name] --provider tailscale
comfy_runner.py tunnel stop [name]

# View/set tunnel configuration
comfy_runner.py tunnel config --provider ngrok --authtoken <token>
comfy_runner.py tunnel config --region us
comfy_runner.py tunnel config --add-domain example.ngrok.io
comfy_runner.py tunnel config --rm-domain example.ngrok.io
```

### Tailscale Serve

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
comfy_runner.py server --tailscale         # expose via tailscale
comfy_runner.py server --tunnels           # enable tunnels
comfy_runner.py server --keep-instances    # keep instances running on shutdown
```

### Key Details

- Routes are prefixed with `/<name>/` for installation-specific operations.
- Long-running operations (deploy, restart, snapshot restore, node add/rm) run in background threads and return a `job_id`.
- Poll `GET /job/<job_id>` to check job status.
- All responses are JSON: `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.

## Global Flags

| Flag | Description |
|------|-------------|
| `--json` | Machine-readable JSON output for all commands |
