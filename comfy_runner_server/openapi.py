"""Auto-generated OpenAPI 3.0.3 spec for the comfy-runner control server.

No external dependencies — ``build_spec()`` returns a plain Python dict
ready for ``flask.jsonify``.
"""

from __future__ import annotations

from typing import Any


# ── Reusable fragments ────────────────────────────────────────────────

_NAME_PARAM: dict[str, Any] = {
    "name": "name",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Installation name (e.g. 'main')",
}

_JOB_ID_PARAM: dict[str, Any] = {
    "name": "job_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Background job ID (hex string)",
}

_SNAPSHOT_ID_PARAM: dict[str, Any] = {
    "name": "snapshot_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Snapshot filename, #index, or partial match",
}

_OTHER_ID_PARAM: dict[str, Any] = {
    "name": "other_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Second snapshot identifier for comparison",
}


def _ok_response(desc: str, extra_props: dict[str, Any] | None = None) -> dict:
    props: dict[str, Any] = {"ok": {"type": "boolean", "example": True}}
    if extra_props:
        props.update(extra_props)
    return {
        "200": {
            "description": desc,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": props,
                    }
                }
            },
        }
    }


def _async_response(desc: str) -> dict:
    return _ok_response(desc, {
        "job_id": {"type": "string", "description": "Background job ID to poll"},
        "async": {"type": "boolean", "enum": [True]},
    })


def _error_responses() -> dict:
    schema = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "enum": [False]},
            "error": {"type": "string"},
        },
    }
    return {
        "400": {
            "description": "Bad request",
            "content": {"application/json": {"schema": schema}},
        },
        "404": {
            "description": "Not found",
            "content": {"application/json": {"schema": schema}},
        },
    }


# ── Route definitions ────────────────────────────────────────────────

_ROUTES: list[dict[str, Any]] = [
    # ── Jobs ──────────────────────────────────────────────────────
    {
        "path": "/jobs",
        "method": "get",
        "tags": ["Jobs"],
        "summary": "List background jobs",
        "description": (
            "Returns all active and recently-completed background jobs. "
            "Completed jobs are garbage-collected after ~10 minutes."
        ),
        "responses": _ok_response("Job list", {
            "jobs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "status": {"type": "string", "enum": ["running", "done", "error", "cancelled"]},
                        "result": {"type": "object", "nullable": True},
                        "error": {"type": "string", "nullable": True},
                        "started_at": {"type": "number"},
                        "finished_at": {"type": "number", "nullable": True},
                    },
                },
            },
        }),
    },
    {
        "path": "/job/{job_id}",
        "method": "get",
        "tags": ["Jobs"],
        "summary": "Poll a background job",
        "description": (
            "Returns full details of a single job including output lines. "
            "Poll this endpoint to track async operation progress."
        ),
        "parameters": [_JOB_ID_PARAM],
        "responses": _ok_response("Job details", {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "status": {"type": "string", "enum": ["running", "done", "error", "cancelled"]},
            "result": {"type": "object", "nullable": True},
            "error": {"type": "string", "nullable": True},
            "output": {"type": "array", "items": {"type": "string"}},
            "started_at": {"type": "number"},
            "finished_at": {"type": "number", "nullable": True},
        }),
    },
    {
        "path": "/job/{job_id}/cancel",
        "method": "post",
        "tags": ["Jobs"],
        "summary": "Cancel a running job",
        "description": "Signals cancellation for a running background job. Only works if the job is still running.",
        "parameters": [_JOB_ID_PARAM],
        "responses": _ok_response("Job cancelled"),
    },

    # ── System info ───────────────────────────────────────────────
    {
        "path": "/system-info",
        "method": "get",
        "tags": ["System"],
        "summary": "System and hardware information",
        "description": (
            "Returns host system details including OS, CPU, memory, NVIDIA driver version, "
            "and GPU information. Useful for checking CUDA compatibility before deploying."
        ),
        "responses": _ok_response("System info", {
            "system_info": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string"},
                    "arch": {"type": "string"},
                    "os_distro": {"type": "string"},
                    "os_release": {"type": "string"},
                    "cpu_model": {"type": "string"},
                    "cpu_cores": {"type": "integer"},
                    "total_memory_gb": {"type": "number"},
                    "nvidia_driver_version": {"type": "string", "nullable": True},
                    "gpus": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "model": {"type": "string"},
                                "vram_mb": {"type": "integer"},
                                "driver_version": {"type": "string"},
                            },
                        },
                    },
                    "installation_count": {"type": "integer"},
                },
            },
        }),
    },

    # ── Installations ─────────────────────────────────────────────
    {
        "path": "/installations",
        "method": "get",
        "tags": ["Installations"],
        "summary": "List all installations",
        "description": "Returns every configured installation with its current process status, port, tunnel URL, etc.",
        "responses": _ok_response("Installation list", {
            "installations": {
                "type": "array",
                "items": {"type": "object", "description": "Installation record with embedded _status"},
            },
        }),
    },
    {
        "path": "/status",
        "method": "get",
        "tags": ["Installations"],
        "summary": "Aggregate status",
        "description": (
            "Returns process status for all installations. "
            "Top-level 'running' is true if any installation is running (backwards-compat)."
        ),
        "responses": _ok_response("Aggregate status", {
            "running": {"type": "boolean"},
            "installations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "running": {"type": "boolean"},
                        "port": {"type": "integer"},
                        "pid": {"type": "integer"},
                    },
                },
            },
        }),
    },

    # ── Per-installation: Process ─────────────────────────────────
    {
        "path": "/{name}/status",
        "method": "get",
        "tags": ["Process"],
        "summary": "Installation status",
        "description": "Returns process status for a single installation (running, port, pid, serve_url, tunnel_url).",
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Status", {
            "running": {"type": "boolean"},
            "port": {"type": "integer"},
            "pid": {"type": "integer"},
            "serve_url": {"type": "string", "description": "Tailscale serve URL (if active)"},
            "tunnel_url": {"type": "string", "description": "Tunnel URL (if active)"},
        }),
    },
    {
        "path": "/{name}/restart",
        "method": "post",
        "tags": ["Process"],
        "summary": "Restart installation",
        "description": "Stops then starts the installation. Async — returns a job_id.",
        "parameters": [_NAME_PARAM],
        "responses": _async_response("Restart queued"),
    },
    {
        "path": "/{name}/stop",
        "method": "post",
        "tags": ["Process"],
        "summary": "Stop installation",
        "description": "Stops a running installation. Synchronous — returns immediately.",
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Stopped", {
            "was_running": {"type": "boolean"},
            "output": {"type": "array", "items": {"type": "string"}},
        }),
    },
    {
        "path": "/{name}",
        "method": "delete",
        "tags": ["Installations"],
        "summary": "Remove installation",
        "description": (
            "Stops the installation if running and removes it from config. "
            "Files on disk are NOT deleted."
        ),
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Removed", {
            "removed": {"type": "boolean"},
            "output": {"type": "array", "items": {"type": "string"}},
        }),
    },
    {
        "path": "/{name}/config",
        "method": "put",
        "tags": ["Installations"],
        "summary": "Update installation config",
        "description": "Updates allowed configuration keys on the installation record.",
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "launch_args": {
                                "type": "string",
                                "description": "CLI arguments passed to ComfyUI on start",
                            },
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Config updated", {
            "updated": {"type": "object", "description": "Map of keys that were changed"},
        }),
    },

    # ── Deploy ────────────────────────────────────────────────────
    {
        "path": "/{name}/deploy",
        "method": "post",
        "tags": ["Deploy"],
        "summary": "Deploy PR/branch/tag/commit or reset",
        "description": (
            "Deploys a code change to the installation. Exactly one of pr, branch, tag, commit, "
            "or reset must be specified. If the installation doesn't exist, it is auto-initialized. "
            "Async — returns a job_id."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "pr": {"type": "integer", "description": "PR number to deploy"},
                            "branch": {"type": "string", "description": "Branch name to deploy"},
                            "tag": {"type": "string", "description": "Git tag to deploy"},
                            "commit": {"type": "string", "description": "Commit SHA to deploy"},
                            "reset": {"type": "boolean", "description": "Reset to original ref"},
                            "start": {"type": "boolean", "description": "Start after deploy (auto-starts if was running)"},
                            "launch_args": {"type": "string", "description": "Override launch args for this deploy"},
                            "cuda_compat": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "Auto-detect host NVIDIA driver and swap torch CUDA build if needed during auto-init. "
                                    "Only applies when the installation doesn't exist yet and is created automatically."
                                ),
                            },
                        },
                    }
                }
            },
        },
        "responses": _async_response("Deploy queued"),
    },

    # ── Nodes ─────────────────────────────────────────────────────
    {
        "path": "/{name}/nodes",
        "method": "get",
        "tags": ["Nodes"],
        "summary": "List custom nodes",
        "description": "Scans the installation's custom_nodes directory and returns all found nodes.",
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Node list", {
            "nodes": {"type": "array", "items": {"type": "object"}},
        }),
    },
    {
        "path": "/{name}/nodes",
        "method": "post",
        "tags": ["Nodes"],
        "summary": "Custom node action",
        "description": (
            "Perform an action on custom nodes. 'add' and 'rm' are async (return job_id). "
            "'enable' and 'disable' are synchronous."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["add", "rm", "enable", "disable"],
                                "description": "Action to perform",
                            },
                            "source": {
                                "type": "string",
                                "description": "Git URL or CNR identifier (required for 'add')",
                            },
                            "node_name": {
                                "type": "string",
                                "description": "Node directory name (required for 'rm', 'enable', 'disable')",
                            },
                            "version": {
                                "type": "string",
                                "description": "CNR version constraint (optional, for 'add')",
                            },
                        },
                    }
                }
            },
        },
        "responses": {
            **_async_response("Action queued (add/rm) or completed (enable/disable)"),
            **_error_responses(),
        },
    },

    # ── Logs ──────────────────────────────────────────────────────
    {
        "path": "/{name}/logs",
        "method": "get",
        "tags": ["Logs"],
        "summary": "Get installation logs",
        "description": (
            "Returns log content. Use 'after' for streaming (byte-offset polling) "
            "or 'lines' to tail the log."
        ),
        "parameters": [
            _NAME_PARAM,
            {
                "name": "after",
                "in": "query",
                "required": False,
                "schema": {"type": "integer"},
                "description": "Byte offset — returns only content after this position (for polling)",
            },
            {
                "name": "lines",
                "in": "query",
                "required": False,
                "schema": {"type": "integer"},
                "description": "Maximum number of lines to return (tail mode)",
            },
        ],
        "responses": _ok_response("Log content", {
            "content": {"type": "string", "description": "Log text"},
            "offset": {"type": "integer", "description": "Current byte offset (use as 'after' for next poll)"},
        }),
    },
    {
        "path": "/{name}/logs/sessions",
        "method": "get",
        "tags": ["Logs"],
        "summary": "List log sessions",
        "description": "Returns all available log session files for the installation.",
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Session list", {
            "sessions": {"type": "array", "items": {"type": "object"}},
        }),
    },

    # ── Tunnels ───────────────────────────────────────────────────
    {
        "path": "/{name}/tunnel/start",
        "method": "post",
        "tags": ["Tunnels"],
        "summary": "Start tunnel",
        "description": "Starts an ngrok or tailscale tunnel for the installation. Requires --tunnels flag on server.",
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "provider": {
                                "type": "string",
                                "enum": ["ngrok", "tailscale"],
                                "default": "tailscale",
                                "description": "Tunnel provider",
                            },
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Tunnel started", {
            "url": {"type": "string", "description": "Public tunnel URL"},
            "output": {"type": "array", "items": {"type": "string"}},
        }),
    },
    {
        "path": "/{name}/tunnel/stop",
        "method": "post",
        "tags": ["Tunnels"],
        "summary": "Stop tunnel",
        "description": "Stops the active tunnel for the installation.",
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Tunnel stopped", {
            "output": {"type": "array", "items": {"type": "string"}},
        }),
    },

    # ── Snapshots ─────────────────────────────────────────────────
    {
        "path": "/{name}/snapshot",
        "method": "get",
        "tags": ["Snapshots"],
        "summary": "List snapshots",
        "description": "Returns summary info for all snapshots of the installation.",
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Snapshot list", {
            "snapshots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string"},
                        "createdAt": {"type": "string"},
                        "trigger": {"type": "string"},
                        "label": {"type": "string", "nullable": True},
                        "nodeCount": {"type": "integer"},
                        "pipPackageCount": {"type": "integer"},
                    },
                },
            },
            "totalCount": {"type": "integer"},
        }),
    },
    {
        "path": "/{name}/snapshot/{snapshot_id}",
        "method": "get",
        "tags": ["Snapshots"],
        "summary": "Show snapshot details",
        "description": "Returns full snapshot data including custom nodes and pip packages.",
        "parameters": [_NAME_PARAM, _SNAPSHOT_ID_PARAM],
        "responses": _ok_response("Snapshot details", {
            "filename": {"type": "string"},
            "snapshot": {"type": "object", "description": "Full snapshot data"},
        }),
    },
    {
        "path": "/{name}/snapshot/{snapshot_id}/diff",
        "method": "get",
        "tags": ["Snapshots"],
        "summary": "Diff snapshot vs current state",
        "description": "Compares a snapshot against the current installation state.",
        "parameters": [_NAME_PARAM, _SNAPSHOT_ID_PARAM],
        "responses": _ok_response("Diff result", {
            "diff": {"type": "object", "description": "Structured diff of nodes and packages"},
        }),
    },
    {
        "path": "/{name}/snapshot/{snapshot_id}/diff/{other_id}",
        "method": "get",
        "tags": ["Snapshots"],
        "summary": "Diff two snapshots",
        "description": "Compares two snapshots against each other.",
        "parameters": [_NAME_PARAM, _SNAPSHOT_ID_PARAM, _OTHER_ID_PARAM],
        "responses": _ok_response("Diff result", {
            "diff": {"type": "object", "description": "Structured diff of nodes and packages"},
        }),
    },
    {
        "path": "/{name}/snapshot/{snapshot_id}/export",
        "method": "get",
        "tags": ["Snapshots"],
        "summary": "Export snapshot as JSON envelope",
        "description": "Returns a portable JSON envelope that can be imported into another installation.",
        "parameters": [_NAME_PARAM, _SNAPSHOT_ID_PARAM],
        "responses": _ok_response("Export envelope", {
            "envelope": {"type": "object", "description": "Portable snapshot envelope"},
        }),
    },
    {
        "path": "/{name}/snapshot/save",
        "method": "post",
        "tags": ["Snapshots"],
        "summary": "Capture snapshot",
        "description": "Captures the current state of the installation as a new snapshot.",
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Optional human-readable label"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Snapshot saved", {
            "filename": {"type": "string"},
        }),
    },
    {
        "path": "/{name}/snapshot/restore",
        "method": "post",
        "tags": ["Snapshots"],
        "summary": "Restore snapshot",
        "description": (
            "Restores the installation to the state captured in a snapshot. "
            "Stops the process if running. Async — returns a job_id."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Snapshot filename, #index, or partial match",
                            },
                        },
                    }
                }
            },
        },
        "responses": _async_response("Restore queued"),
    },
    {
        "path": "/{name}/snapshot/import",
        "method": "post",
        "tags": ["Snapshots"],
        "summary": "Import and auto-restore snapshot",
        "description": (
            "Imports a snapshot envelope and optionally restores the first imported snapshot. "
            "If restore=true (default), the restore runs async and returns a job_id."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["envelope"],
                        "properties": {
                            "envelope": {"type": "object", "description": "Snapshot export envelope"},
                            "restore": {
                                "type": "boolean",
                                "default": True,
                                "description": "Auto-restore after import (default true)",
                            },
                        },
                    }
                }
            },
        },
        "responses": _async_response("Import complete, restore queued"),
    },

    # ── Models ────────────────────────────────────────────────────
    {
        "path": "/{name}/download-model",
        "method": "post",
        "tags": ["Models"],
        "summary": "Download a model",
        "description": (
            "Downloads a single model file by URL into the specified directory. "
            "Skips if the file already exists. Async — returns a job_id."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["url", "directory"],
                        "properties": {
                            "url": {"type": "string", "description": "Download URL"},
                            "directory": {"type": "string", "description": "Target subdirectory under models/ (e.g. 'checkpoints')"},
                            "name": {"type": "string", "description": "Override filename (auto-derived from URL if omitted)"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Download queued"),
    },
    {
        "path": "/{name}/workflow-models",
        "method": "post",
        "tags": ["Models"],
        "summary": "Download workflow models",
        "description": (
            "Parses a ComfyUI workflow JSON, identifies all required models, "
            "and downloads any that are missing. Async — returns a job_id."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["workflow"],
                        "properties": {
                            "workflow": {"type": "object", "description": "ComfyUI workflow JSON (API format)"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Download queued"),
    },

    # ── Backwards-compat un-prefixed routes ───────────────────────
    {
        "path": "/deploy",
        "method": "post",
        "tags": ["Deploy"],
        "summary": "Deploy (default installation)",
        "description": "Same as POST /{name}/deploy but targets the first configured installation.",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "pr": {"type": "integer"},
                            "branch": {"type": "string"},
                            "tag": {"type": "string"},
                            "commit": {"type": "string"},
                            "reset": {"type": "boolean"},
                            "start": {"type": "boolean"},
                            "launch_args": {"type": "string"},
                            "cuda_compat": {"type": "boolean", "default": False},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Deploy queued"),
    },
    {
        "path": "/restart",
        "method": "post",
        "tags": ["Process"],
        "summary": "Restart (default installation)",
        "description": "Same as POST /{name}/restart but targets the first configured installation.",
        "responses": _async_response("Restart queued"),
    },
    {
        "path": "/stop",
        "method": "post",
        "tags": ["Process"],
        "summary": "Stop (default installation)",
        "description": "Same as POST /{name}/stop but targets the first configured installation.",
        "responses": _ok_response("Stopped", {
            "was_running": {"type": "boolean"},
        }),
    },
]


# ── Spec builder ──────────────────────────────────────────────────────

def build_spec() -> dict[str, Any]:
    """Assemble a complete OpenAPI 3.0.3 specification dict."""

    paths: dict[str, dict[str, Any]] = {}
    tag_set: list[str] = []

    for route in _ROUTES:
        path = route["path"]
        method = route["method"]

        operation: dict[str, Any] = {
            "summary": route["summary"],
            "description": route["description"],
            "tags": route.get("tags", []),
            "operationId": route.get("operationId", f"{method}_{path.replace('/', '_').replace('{', '').replace('}', '').strip('_')}"),
        }

        if "parameters" in route:
            operation["parameters"] = route["parameters"]

        if "requestBody" in route:
            operation["requestBody"] = route["requestBody"]

        responses = dict(route.get("responses", {}))
        for code, body in _error_responses().items():
            responses.setdefault(code, body)
        operation["responses"] = responses

        for tag in route.get("tags", []):
            if tag not in tag_set:
                tag_set.append(tag)

        paths.setdefault(path, {})[method] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "comfy-runner Control API",
            "version": "1.0.0",
            "description": (
                "HTTP API for managing ComfyUI installations via comfy-runner.\n\n"
                "## Response envelope\n\n"
                "All responses return JSON with an `ok` boolean:\n"
                "- **Success**: `{\"ok\": true, ...}`\n"
                "- **Failure**: `{\"ok\": false, \"error\": \"...\"}`\n\n"
                "## Async operations\n\n"
                "Long-running operations (deploy, restart, snapshot restore, node add/rm, "
                "model downloads) run in background threads and return immediately with:\n"
                "```json\n"
                "{\"ok\": true, \"job_id\": \"abc123\", \"async\": true}\n"
                "```\n"
                "Poll `GET /job/{job_id}` to track progress. The job object includes "
                "`status` (running/done/error/cancelled), `result`, `error`, and `output` "
                "(array of log lines). Cancel with `POST /job/{job_id}/cancel`.\n\n"
                "## Installations\n\n"
                "Most routes are prefixed with `/{name}/` where `name` is the installation "
                "name (e.g. `main`). Each installation is an independent ComfyUI checkout "
                "with its own process, config, custom nodes, and snapshots. Use "
                "`GET /installations` to discover available names.\n\n"
                "A few legacy un-prefixed routes (`/deploy`, `/restart`, `/stop`) target "
                "the first configured installation."
            ),
        },
        "tags": [{"name": t} for t in tag_set],
        "paths": paths,
    }
