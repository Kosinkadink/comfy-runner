# comfy-runner — Agent Guide

See `README.md` for full CLI usage, HTTP API endpoints, code layout, and setup instructions.

## Quick tips

- The running server exposes an OpenAPI spec at `GET /openapi.json` — fetch it to discover all available endpoints and parameters.
- When connecting to a remote comfy-runner server via Tailscale, you **must** use the full MagicDNS FQDN (e.g. `https://mybox.tailnet-name.ts.net:9189`), not the short hostname. See the Tailscale section in `README.md` for details.

## Path traversal prevention

Any time user-supplied input or external API data is used as part of a file path, **always sanitize it** to prevent path traversal attacks:

- Use `is_safe_path_component(name)` from `safe_file.py` to validate bare filenames and directory names. This rejects empty strings, `.`, `..`, and any value containing path separators.
- **Never** use `Path(value).name != value` alone — it does not reject `..` (since `Path("..").name == ".."`).
- For paths that must stay within a target directory, **resolve and verify** with `resolved.is_relative_to(base_dir.resolve())`.
- Use `Path(value).name` to **strip** directory components from untrusted filenames (e.g. API responses), but always also check the result with `is_safe_path_component`.
- See `safe_file.py` (`is_safe_path_component`), `comfy_runner/testing/client.py` (`download_output`), and `comfy_runner/nodes.py` (`_safe_extract`) for canonical examples.

This applies to: filenames from HTTP responses, user CLI arguments used as directory/file names, ZIP entry names, model paths, and URL path parameters in server endpoints.

## When adding or changing server endpoints

Always update `comfy_runner_server/openapi.py` — add a new entry to the `_ROUTES` list for any new endpoint, or update the existing entry if changing an endpoint's schema. The spec is auto-served at `GET /openapi.json` from this file.

## Station subcommand

The `station` subcommand lets users interact with a central comfy-runner fleet server. It reads `station.json` (walks up from cwd) for the server URL and uses `RemoteRunner` for HTTP calls. Key commands:

- `station info` — show config + verify connectivity
- `station pods [list|create|deploy|start|stop|terminate]` — pod lifecycle via `/pods/*` endpoints
- `station tests [list|run|fleet|status|report]` — test orchestration via `/tests/*` endpoints
- `station dashboard` — open fleet dashboard
- `station jobs` — list active jobs

The station commands talk to the central server's orchestration API (`/pods/*`, `/tests/*`). Direct pod interaction (deploy, status, logs) uses existing `hosted` commands via Tailscale.

## Testing subsystem

The `test` CLI subcommand runs regression tests against ComfyUI instances:

- `test run <suite> --target <spec>` — single target test
- `test fleet <suite> --target <spec> [--target ...]` — parallel fleet test
- `test list` — discover test suites
- `test baseline` — approve outputs as baselines
- `test report` — regenerate reports

Target specs: `local:<url>`, `remote:<server_url>`, `runpod:<gpu_type>`.

Key modules: `comfy_runner/testing/` (client, runner, suite, fleet, runpod, report, compare/).
