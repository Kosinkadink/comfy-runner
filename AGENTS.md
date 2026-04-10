# comfy-runner — Agent Guide

This repo provides a toolkit for managing ComfyUI installations: init, deploy, process control, tunneling, snapshots, custom nodes, and hosted GPU deployments.

## Key Entry Points

- **CLI**: `comfy_runner_cli/cli.py` — all commands and argument parsing
- **HTTP API server**: `comfy_runner_server/server.py` — Flask app with REST endpoints
- **Core library**: `comfy_runner/` — shared logic used by both CLI and server

## Remote Server API (most common agent task)

The server runs on port **9189** by default. When the server uses `--tailscale`, all requests must use **HTTPS with the full MagicDNS FQDN** (e.g. `https://mybox.tailnet-name.ts.net:9189`). Short hostnames will fail TLS handshake.

### Discovering the FQDN

```bash
tailscale status --json | python -c "import json,sys; print(json.load(sys.stdin)['MagicDNSSuffix'])"
```

Then construct: `https://<hostname>.<suffix>:9189`

### Common API patterns

All responses are JSON: `{"ok": true, ...}` or `{"ok": false, "error": "..."}`.

Long-running operations return `{"job_id": "..."}` — poll `GET /job/<id>` until `status` is `done` or `error`.

| Task | Method | Endpoint | Body |
|------|--------|----------|------|
| List installations | `GET` | `/installations` | — |
| Installation info | `GET` | `/<name>/info` | — |
| Deploy latest master | `POST` | `/<name>/deploy` | `{"latest": true}` |
| Deploy a PR | `POST` | `/<name>/deploy` | `{"pr": 1234}` |
| Deploy a branch | `POST` | `/<name>/deploy` | `{"branch": "my-branch"}` |
| Deploy a commit | `POST` | `/<name>/deploy` | `{"commit": "abc123"}` |
| Pull current branch | `POST` | `/<name>/deploy` | `{"pull": true}` |
| Set launch args | `PUT` | `/<name>/config` | `{"launch_args": "--enable-manager --cpu"}` |
| Start | `POST` | `/<name>/start` | — |
| Stop | `POST` | `/<name>/stop` | — |
| Restart | `POST` | `/<name>/restart` | — |
| Check status | `GET` | `/<name>/status` | — |
| Poll a job | `GET` | `/job/<job_id>` | — |

### Deploy + launch_args in one call

The deploy endpoint accepts `"launch_args"` in the body to update startup arguments atomically:

```json
{
  "latest": true,
  "launch_args": "--enable-manager --enable-manager-legacy-ui --cuda-device 0"
}
```

If the instance was running before deploy, it auto-restarts with the new args.

## Code Layout

| Directory | Purpose |
|-----------|---------|
| `comfy_runner/config.py` | Installation records, global config (shared_dir, tokens) |
| `comfy_runner/process.py` | Start/stop/status of ComfyUI processes |
| `comfy_runner/deployments.py` | Git-based deploy logic (PR, branch, tag, commit, reset) |
| `comfy_runner/installations.py` | Init/remove installations, list |
| `comfy_runner/tunnel.py` | ngrok and Tailscale tunnel management |
| `comfy_runner/snapshot.py` | Snapshot capture/restore/diff |
| `comfy_runner/nodes.py` | Custom node add/remove/enable/disable |
| `comfy_runner/hosted/` | RunPod cloud provider (volumes, pods, remote API) |
| `comfy_runner_server/server.py` | HTTP API — all route definitions |
| `comfy_runner_server/openapi.py` | Auto-generated OpenAPI 3.0.3 spec |

## Full documentation

See `README.md` for complete CLI usage, API endpoint table, setup instructions, and hosted deployment guide.
