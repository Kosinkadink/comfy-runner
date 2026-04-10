# comfy-runner — Agent Guide

See `README.md` for full CLI usage, HTTP API endpoints, code layout, and setup instructions.

## Quick tips

- The running server exposes an OpenAPI spec at `GET /openapi.json` — fetch it to discover all available endpoints and parameters.
- When connecting to a remote comfy-runner server via Tailscale, you **must** use the full MagicDNS FQDN (e.g. `https://mybox.tailnet-name.ts.net:9189`), not the short hostname. See the Tailscale section in `README.md` for details.
