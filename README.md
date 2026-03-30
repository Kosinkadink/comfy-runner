# comfy-runner

A Python toolkit for managing ComfyUI instances — installation, snapshot, process management, tunneling, and remote execution.

## Packages

- **`comfy_runner/`** — Core library (config, environment, process management, tunneling, shared paths)
- **`comfy_runner_cli/`** — CLI interface (`python -m comfy_runner_cli` or `python comfy_runner.py`)
- **`comfy_runner_server/`** — HTTP API server (`python -m comfy_runner_server`)

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip.exe install -r requirements.txt
# Linux/macOS
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
python comfy_runner.py              # CLI
python -m comfy_runner_cli          # CLI (module)
python -m comfy_runner_server       # HTTP server
```
