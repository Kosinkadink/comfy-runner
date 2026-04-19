---
name: using-comfy-runner
description: "Manages ComfyUI instances via comfy-runner: install, start/stop, deploy PRs/branches, snapshots, custom nodes, tunneling, model downloads, hosted GPU pods (RunPod), and remote server API. Use when asked to deploy ComfyUI, manage installations, expose via Tailscale/ngrok, or interact with a remote comfy-runner server."
---

# Using comfy-runner

comfy-runner is a Python toolkit for managing ComfyUI instances — installation, snapshot, process management, tunneling, and remote execution. See `comfy-runner/README.md` for full details.

## Setup

**Always check the venv exists before using it:**

```powershell
# Windows — check venv exists
Test-Path comfy-runner\.venv\Scripts\python.exe

# Linux/macOS — check venv exists
test -f comfy-runner/.venv/bin/python
```

If the venv is missing, create it first:

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File comfy-runner\setup_env.ps1

# Linux/macOS
chmod +x comfy-runner/setup_env.sh && comfy-runner/setup_env.sh
```

> **Windows note:** Always use `powershell -ExecutionPolicy Bypass -File` to run `.ps1` setup scripts. The default execution policy may block unsigned scripts with a `SecurityError`.

Once the venv exists, run commands with:

```powershell
# Windows
comfy-runner\.venv\Scripts\python.exe comfy-runner\comfy_runner.py <command>

# Linux/macOS
comfy-runner/.venv/bin/python comfy-runner/comfy_runner.py <command>
```

### GitHub Token

Set `GITHUB_TOKEN` for GitHub API access (releases, manifests). Try these sources in order:

1. **`githubtoken.txt`** — Search the workspace root and parent directories. Use `Test-Path` (Windows) or `test -f` (Unix) to verify. Do not assume a hardcoded path.
   ```powershell
   $env:GITHUB_TOKEN = (Get-Content githubtoken.txt -Raw).Trim()
   ```

2. **`gh` CLI** (if installed):
   ```powershell
   $env:GITHUB_TOKEN = (gh auth token)
   ```

3. **Git credential manager** — Falls back to the OS credential store (works on Windows with Git Credential Manager, macOS with Keychain):
   ```powershell
   # Windows (PowerShell)
   $env:GITHUB_TOKEN = ("protocol=https`nhost=github.com`n" | git credential fill | Select-String "password=(.+)" | ForEach-Object { $_.Matches[0].Groups[1].Value })
   ```
   ```bash
   # Linux/macOS
   export GITHUB_TOKEN=$(echo -e "protocol=https\nhost=github.com\n" | git credential fill | grep ^password= | cut -d= -f2)
   ```

After setting the token, run commands and clean up:
```powershell
comfy-runner\.venv\Scripts\python.exe comfy-runner\comfy_runner.py <command>; Remove-Item Env:\GITHUB_TOKEN
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

   **Windows (PowerShell):**
   ```powershell
   $tsStatus = tailscale status --json | ConvertFrom-Json
   $tsStatus.MagicDNSSuffix
   ```

   **Linux/macOS:**
   ```bash
   tailscale status --json | python3 -c "import json,sys; print(json.load(sys.stdin)['MagicDNSSuffix'])"
   ```

   > **Do NOT** pipe `tailscale status --json` to `python -c` on Windows — PowerShell's pipeline encoding (UTF-8 BOM) causes `JSONDecodeError`. Use `ConvertFrom-Json` instead, which is a native PowerShell cmdlet and avoids encoding issues entirely.

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

## Remote Server Operations (HTTP API)

The local CLI `deploy` command only works on **local** installations. To deploy to a **remote** comfy-runner server (e.g., over Tailscale), use the HTTP API directly — there is no `remote deploy` CLI command.

### Remote deploy workflow

1. **Resolve the server URL** (see Tailscale Resolution above).

2. **Check current state** before deploying (to preserve `launch_args`):

   **Windows (PowerShell):**
   ```powershell
   Invoke-RestMethod -Uri "https://mybox.tailnet.ts.net:9189/instance-name/info"
   ```

   **Linux/macOS:**
   ```bash
   curl -s https://mybox.tailnet.ts.net:9189/instance-name/info
   ```

3. **Trigger the deploy:**

   **Windows (PowerShell):**
   ```powershell
   $body = @{ pr = 1234 } | ConvertTo-Json
   $resp = Invoke-RestMethod -Uri "https://mybox.tailnet.ts.net:9189/instance-name/deploy" -Method Post -Body $body -ContentType "application/json"
   $resp.job_id   # deploy is async — save the job_id
   ```

   **Linux/macOS:**
   ```bash
   curl -s -X POST https://mybox.tailnet.ts.net:9189/instance-name/deploy \
     -H "Content-Type: application/json" -d '{"pr": 1234}'
   ```

   The deploy body accepts: `pr`, `branch`, `tag`, `commit`, `latest` (bool), `pull` (bool), `reset` (bool), `launch_args`, `github_token`.

4. **Poll the job until complete:**

   ```powershell
   Invoke-RestMethod -Uri "https://mybox.tailnet.ts.net:9189/job/$($resp.job_id)"
   ```

   Repeat until `status` is `"completed"` or `"failed"`.

### Other common remote operations

| Operation | Method | Endpoint |
|-----------|--------|----------|
| List installations | GET | `/installations` |
| Installation status | GET | `/{name}/status` |
| Start | POST | `/{name}/start` |
| Stop | POST | `/{name}/stop` |
| View logs | GET | `/{name}/logs?lines=50` |
| Self-update server | POST | `/self-update` |

**For the full endpoint list, read the "API Endpoints" section in `comfy-runner/README.md`.** If the server is running, `GET /openapi.json` has the complete auto-generated spec.

## CLI Quick Reference (Local Only)

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

3. **Local CLI vs remote API.** The `deploy`, `start`, `stop`, etc. CLI commands operate on **local** installations only. For remote servers, use the HTTP API (`Invoke-RestMethod` on Windows, `curl` on Unix). There is no `remote deploy` CLI subcommand — only `remote upload-model` exists.

4. **`--cuda-compat` for cloud GPUs.** The standalone environment bundles PyTorch for CUDA 13.0 (driver ≥ 580). Use `--cuda-compat` on `init`/`hosted init` to auto-detect and reinstall a compatible PyTorch.

5. **`--json` flag.** All CLI commands support `--json` for machine-readable output.

6. **OpenAPI spec.** When unsure about API parameters, fetch `/openapi.json` from the running server.

## Windows PowerShell Notes

Amp on Windows always runs commands in **PowerShell 7 (pwsh)**, regardless of whether the user launched Amp from `cmd.exe`, Windows PowerShell 5.1, or pwsh. This means:

- **`&&` works** — PS 7 supports pipeline chain operators (`&&`, `||`). Do not avoid them or use `;` as a workaround (`;` runs the second command even if the first fails).
- **`ConvertFrom-Json` / `ConvertTo-Json`** — Use these native cmdlets to parse and build JSON. Do not pipe JSON through `python -c` on Windows — PowerShell's pipeline encoding adds a UTF-8 BOM that causes `JSONDecodeError` in Python.
- **`Invoke-RestMethod`** — Use this for HTTP API calls on Windows. It automatically parses JSON responses into PowerShell objects. Equivalent to `curl` on Unix.
- **Execution policy** — Always run `.ps1` scripts with `powershell -ExecutionPolicy Bypass -File <script>` to avoid `SecurityError` from the default policy.
- **Environment variables** — Use `$env:VAR_NAME` syntax (not `%VAR_NAME%` which is `cmd.exe` syntax).

> **PS 7 vs PS 5.1:** Older Windows PowerShell 5.1 (`powershell.exe`) does NOT support `&&` and has different encoding defaults. Amp does not use PS 5.1, so do not write workarounds for it.
