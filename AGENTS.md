# comfy-runner — Agent Guide

See `README.md` for full CLI usage, HTTP API endpoints, code layout, and setup instructions.

## Quick tips

- The running server exposes an OpenAPI spec at `GET /openapi.json` — fetch it to discover all available endpoints and parameters.
- When connecting to a remote comfy-runner server via Tailscale, you **must** use the full MagicDNS FQDN (e.g. `https://mybox.tailnet-name.ts.net:9189`), not the short hostname. See the Tailscale section in `README.md` for details.

## When adding or changing server endpoints

Always update `comfy_runner_server/openapi.py` — add a new entry to the `_ROUTES` list for any new endpoint, or update the existing entry if changing an endpoint's schema. The spec is auto-served at `GET /openapi.json` from this file.
