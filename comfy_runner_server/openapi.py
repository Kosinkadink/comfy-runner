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

_POD_NAME_PARAM: dict[str, Any] = {
    "name": "name",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Pod name (e.g. 'test-l40s')",
}

_TEST_ID_PARAM: dict[str, Any] = {
    "name": "test_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Test run ID (same as the job_id returned by POST /tests/run or /tests/fleet)",
}

_SUITE_NAME_PARAM: dict[str, Any] = {
    "name": "name",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
    "description": "Test suite name",
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
                    "comfy_runner_commit": {"type": "string", "nullable": True, "description": "Short git commit hash of the comfy-runner server"},
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
        "path": "/{name}/unlock",
        "method": "post",
        "tags": ["Process"],
        "summary": "Force-release installation lock",
        "description": (
            "Replaces the in-memory lock for this installation with a fresh one. "
            "Use when an installation is stuck in 'busy' state due to a hung job."
        ),
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Lock reset", {
            "lock_reset": {"type": "boolean", "description": "True if a lock existed and was replaced"},
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

    # ── Info ──────────────────────────────────────────────────────
    {
        "path": "/{name}/info",
        "method": "get",
        "tags": ["Installations"],
        "summary": "Installation info",
        "description": (
            "Returns the full installation record (variant, release, ComfyUI ref, commit, "
            "launch args, deploy tracking) merged with runtime status and tunnel URLs."
        ),
        "parameters": [_NAME_PARAM],
        "responses": _ok_response("Installation info", {
            "name": {"type": "string"},
            "path": {"type": "string"},
            "variant": {"type": "string"},
            "release_tag": {"type": "string"},
            "comfyui_ref": {"type": "string"},
            "head_commit": {"type": "string"},
            "python_version": {"type": "string"},
            "launch_args": {"type": "string"},
            "created_at": {"type": "string"},
            "deployed_pr": {"type": "integer", "nullable": True},
            "deployed_branch": {"type": "string", "nullable": True},
            "deployed_repo": {"type": "string", "nullable": True},
            "deployed_title": {"type": "string", "nullable": True},
            "running": {"type": "boolean"},
            "pid": {"type": "integer"},
            "port": {"type": "integer"},
            "healthy": {"type": "boolean"},
            "serve_url": {"type": "string", "description": "Tailscale serve URL (if active)"},
            "tunnel_url": {"type": "string", "description": "Tunnel URL (if active)"},
        }),
    },

    # ── Deploy ────────────────────────────────────────────────────
    {
        "path": "/{name}/deploy",
        "method": "post",
        "tags": ["Deploy"],
        "summary": "Deploy PR/branch/tag/commit, update to latest release, or pull",
        "description": (
            "Deploys a code change to the installation. Exactly one of pr, branch, tag, commit, "
            "reset, latest, or pull must be specified. If the installation doesn't exist, it is "
            "auto-initialized. Async — returns a job_id."
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
                            "branch": {"type": "string", "description": "Branch name to deploy (persisted for --pull)"},
                            "tag": {"type": "string", "description": "Git tag to deploy"},
                            "commit": {"type": "string", "description": "Commit SHA to deploy"},
                            "reset": {"type": "boolean", "description": "Reset to original ref"},
                            "latest": {
                                "type": "boolean",
                                "description": (
                                    "Update to the latest standalone release's ComfyUI ref. "
                                    "Lightweight — does not re-download the standalone environment."
                                ),
                            },
                            "pull": {
                                "type": "boolean",
                                "description": (
                                    "Re-fetch the currently tracked branch or PR. "
                                    "Errors if no movable target is tracked."
                                ),
                            },
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
                            "variant": {
                                "type": "string",
                                "description": (
                                    "Force a specific variant for auto-init (e.g. 'linux-nvidia', 'win-nvidia'). "
                                    "Only applies when the installation doesn't exist yet."
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
                            "token": {"type": "string", "description": "Bearer token for authenticated downloads (ephemeral, not stored)"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Download queued"),
    },
    {
        "path": "/{name}/upload-model",
        "method": "post",
        "tags": ["Models"],
        "summary": "Upload a model file",
        "description": (
            "Upload a model file via multipart form data. Supports resumable uploads — "
            "check status first to get bytes_received, then re-upload with offset. "
            "Staging files older than 24h are automatically cleaned up."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file", "directory"],
                        "properties": {
                            "file": {"type": "string", "format": "binary", "description": "Model file to upload"},
                            "directory": {"type": "string", "description": "Target subdirectory under models/"},
                            "name": {"type": "string", "description": "Override filename (default: original filename)"},
                            "offset": {"type": "integer", "description": "Byte offset for resuming (default: 0)"},
                            "hash": {"type": "string", "description": "Expected file hash for integrity verification"},
                            "hash_type": {"type": "string", "enum": ["blake3", "sha256"], "description": "Hash algorithm (default: blake3)"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Upload complete", {
            "path": {"type": "string"},
            "size": {"type": "integer"},
            "resumed": {"type": "boolean"},
            "skipped": {"type": "boolean"},
            "hash": {"type": "string", "description": "File hash (computed or verified)"},
            "hash_type": {"type": "string"},
        }),
    },
    {
        "path": "/{name}/upload-model/status",
        "method": "get",
        "tags": ["Models"],
        "summary": "Check upload status",
        "description": (
            "Check if a model file exists or has a partial upload in staging. "
            "Use bytes_received to determine the resume offset."
        ),
        "parameters": [
            _NAME_PARAM,
            {"name": "directory", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "name", "in": "query", "required": True, "schema": {"type": "string"}},
        ],
        "responses": _ok_response("Upload status", {
            "exists": {"type": "boolean"},
            "complete": {"type": "boolean"},
            "bytes_received": {"type": "integer"},
            "path": {"type": "string"},
            "created_at": {"type": "number", "nullable": True},
        }),
    },
    {
        "path": "/{name}/upload-model/status",
        "method": "delete",
        "tags": ["Models"],
        "summary": "Delete partial upload",
        "description": "Remove a stale partial upload from the staging directory.",
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["directory", "name"],
                        "properties": {
                            "directory": {"type": "string"},
                            "name": {"type": "string"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Staging deleted", {
            "removed": {"type": "boolean"},
        }),
    },
    {
        "path": "/{name}/move-model",
        "method": "post",
        "tags": ["Models"],
        "summary": "Move or copy a model",
        "description": (
            "Moves or copies a model file between subdirectories under models/. "
            "Set copy=true to copy instead of move. Fails if the destination already exists."
        ),
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["from_directory", "to_directory", "name"],
                        "properties": {
                            "from_directory": {"type": "string", "description": "Source subdirectory under models/ (e.g. 'diffusion_models')"},
                            "to_directory": {"type": "string", "description": "Destination subdirectory under models/ (e.g. 'checkpoints')"},
                            "name": {"type": "string", "description": "Filename to move or copy"},
                            "copy": {"type": "boolean", "description": "If true, copy instead of move (default: false)"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Model moved/copied", {
            "action": {"type": "string", "enum": ["moved", "copied"]},
            "name": {"type": "string"},
            "from_directory": {"type": "string"},
            "to_directory": {"type": "string"},
        }),
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

    # ── Start ─────────────────────────────────────────────────────
    {
        "path": "/{name}/start",
        "method": "post",
        "tags": ["Process"],
        "summary": "Start installation",
        "description": "Start a stopped installation. Fails if already running.",
        "parameters": [_NAME_PARAM],
        "responses": _async_response("Start queued"),
    },

    # ── ComfyUI Proxy ─────────────────────────────────────────────
    {
        "path": "/{name}/comfyui/{subpath}",
        "method": "get",
        "tags": ["ComfyUI Proxy"],
        "summary": "Proxy GET to ComfyUI",
        "description": "Forward a GET request to the running ComfyUI instance. The subpath is appended to http://127.0.0.1:{port}/.",
        "parameters": [
            _NAME_PARAM,
            {"name": "subpath", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Path to forward to ComfyUI"},
        ],
        "responses": {"200": {"description": "Proxied response from ComfyUI"}},
    },
    {
        "path": "/{name}/comfyui/{subpath}",
        "method": "post",
        "tags": ["ComfyUI Proxy"],
        "summary": "Proxy POST to ComfyUI",
        "description": "Forward a POST request to the running ComfyUI instance (e.g. queue a prompt via /api/prompt).",
        "parameters": [
            _NAME_PARAM,
            {"name": "subpath", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Path to forward to ComfyUI"},
        ],
        "responses": {"200": {"description": "Proxied response from ComfyUI"}},
    },

    # ── Outputs ───────────────────────────────────────────────────
    {
        "path": "/{name}/outputs",
        "method": "get",
        "tags": ["Outputs"],
        "summary": "List output files",
        "description": "List generated output files, sorted by modification time (newest first). Supports prefix filtering, pagination via limit, and time-based filtering via after.",
        "parameters": [
            _NAME_PARAM,
            {"name": "prefix", "in": "query", "schema": {"type": "string"}, "description": "Filter by filename prefix"},
            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}, "description": "Max files to return"},
            {"name": "after", "in": "query", "schema": {"type": "number"}, "description": "Only return files modified after this Unix timestamp"},
        ],
        "responses": _ok_response("Output file list", {
            "output_dir": {"type": "string"},
            "files": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "size": {"type": "integer"},
                "modified": {"type": "number"},
            }}},
        }),
    },
    {
        "path": "/{name}/outputs/{filepath}",
        "method": "get",
        "tags": ["Outputs"],
        "summary": "Download output file",
        "description": "Download a specific output file by path. Returns the raw file content.",
        "parameters": [
            _NAME_PARAM,
            {"name": "filepath", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Relative path to the output file"},
        ],
        "responses": {"200": {"description": "File content", "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
    },

    # ── Rename ─────────────────────────────────────────────────────
    {
        "path": "/{name}/rename",
        "method": "post",
        "tags": ["Installations"],
        "summary": "Rename an installation",
        "description": "Rename an installation. The installation must be stopped first.",
        "parameters": [_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "description": "New installation name"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Renamed", {
            "old_name": {"type": "string"},
            "new_name": {"type": "string"},
        }),
    },

    # ── Self-update ───────────────────────────────────────────────
    {
        "path": "/self-update",
        "method": "post",
        "tags": ["System"],
        "summary": "Update server code and restart",
        "description": (
            "Runs git pull --ff-only on the comfy-runner repo. "
            "If new commits are pulled, the server process restarts automatically (~1-2s downtime). "
            "Returns updated=false if already up to date."
        ),
        "responses": _ok_response("Update result", {
            "updated": {"type": "boolean", "description": "Whether new code was pulled"},
            "message": {"type": "string", "description": "git pull output"},
            "restarting": {"type": "boolean", "description": "Whether the server is restarting (only present if updated=true)"},
        }),
    },

    # ── Global Config ─────────────────────────────────────────────
    {
        "path": "/config",
        "method": "get",
        "tags": ["Config"],
        "summary": "View global config",
        "description": "Returns global configuration including shared_dir and whether auth tokens are configured (booleans, never actual values).",
        "responses": _ok_response("Global config", {
            "config": {"type": "object", "properties": {
                "shared_dir": {"type": "string"},
                "hf_token": {"type": "boolean", "description": "Whether a HuggingFace token is set"},
                "modelscope_token": {"type": "boolean", "description": "Whether a ModelScope token is set"},
            }},
        }),
    },
    {
        "path": "/config",
        "method": "put",
        "tags": ["Config"],
        "summary": "Update global config",
        "description": "Set global configuration values. Token values are stored securely; GET /config only returns booleans.",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "shared_dir": {"type": "string", "description": "Path to shared directory"},
                            "hf_token": {"type": "string", "description": "HuggingFace access token (empty string to clear)"},
                            "modelscope_token": {"type": "string", "description": "ModelScope SDK token (empty string to clear)"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Config updated", {
            "updated": {"type": "object"},
        }),
    },

    # ── Testing ──────────────────────────────────────────────────────
    {
        "path": "/test/run",
        "method": "post",
        "tags": ["Testing"],
        "summary": "Run a test suite (async)",
        "description": (
            "Queue a test suite execution against a running ComfyUI installation. "
            "Returns a job_id immediately — poll GET /job/{job_id} to track progress. "
            "When done, the job result contains the full report and output paths."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["suite"],
                        "properties": {
                            "suite": {"type": "string", "description": "Path to test suite directory on disk"},
                            "name": {"type": "string", "description": "Installation name to test against (default: first installation)"},
                            "timeout": {"type": "integer", "default": 600, "description": "Per-workflow timeout in seconds"},
                            "http_timeout": {"type": "integer", "default": 30, "description": "HTTP request timeout in seconds"},
                            "formats": {"type": "string", "default": "json,html,markdown", "description": "Comma-separated report formats"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Test run queued"),
    },
    {
        "path": "/test/results/{run_id}",
        "method": "get",
        "tags": ["Testing"],
        "summary": "Get test run results",
        "description": (
            "Retrieve results from a previous test run. Pass format=json to get the "
            "full report inline. Requires the suite query parameter to locate the run directory."
        ),
        "parameters": [
            {"name": "run_id", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Run ID (timestamp directory name)"},
            {"name": "suite", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Path to the test suite directory"},
            {"name": "format", "in": "query", "schema": {"type": "string"}, "description": "Return format (json to get report inline)"},
        ],
        "responses": _ok_response("Test results", {
            "run_id": {"type": "string"},
            "output_dir": {"type": "string", "description": "Present when format != json"},
            "files": {"type": "array", "description": "Present when format != json", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "size": {"type": "integer"},
            }}},
            "report": {"type": "object", "description": "Present when format=json"},
        }),
    },
    {
        "path": "/test/suites",
        "method": "get",
        "tags": ["Testing"],
        "summary": "List available test suites",
        "description": "Discover test suites in a directory. Each suite contains workflows and optional baselines for regression testing.",
        "parameters": [
            {"name": "dir", "in": "query", "schema": {"type": "string", "default": "."}, "description": "Directory to search for suites"},
        ],
        "responses": _ok_response("Suite list", {
            "suites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                        "description": {"type": "string"},
                        "workflows": {"type": "integer"},
                        "required_models": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        }),
    },

    # ── Pods (Central Orchestration) ─────────────────────────────────
    {
        "path": "/pods",
        "method": "get",
        "tags": ["Pods"],
        "summary": "List all pods",
        "description": (
            "Returns all RunPod pods from config, merged with live status from the RunPod API. "
            "Includes Tailscale server URLs for pods on the tailnet. "
            "Use the optional ``?purpose=`` query parameter to filter by record purpose "
            "(``pr``, ``persistent``, or ``test``); records with no explicit purpose are "
            "treated as ``persistent`` for filtering."
        ),
        "parameters": [
            {
                "name": "purpose",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["pr", "persistent", "test"]},
                "description": "Filter to pods whose record matches this purpose (case-sensitive).",
            },
        ],
        "responses": _ok_response("Pod list", {
            "pods": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "id": {"type": "string"},
                        "status": {"type": "string", "enum": ["RUNNING", "EXITED", "TERMINATED", "UNKNOWN"]},
                        "gpu_type": {"type": "string"},
                        "datacenter": {"type": "string"},
                        "image": {"type": "string"},
                        "cost_per_hr": {"type": "number"},
                        "server_url": {"type": "string"},
                        "comfy_url": {"type": "string"},
                        "purpose": {"type": "string", "enum": ["pr", "persistent", "test"], "description": "Pod purpose (PR-review pods are subject to the idle reaper)"},
                        "pr_number": {"type": "integer"},
                        "last_active_at": {"type": "integer", "description": "Epoch seconds of last activity"},
                        "idle_timeout_s": {"type": "integer"},
                        "idle_in_s": {"type": "integer", "description": "Seconds remaining before the idle reaper stops this pod (PR pods only)"},
                        "status_hint": {"type": "string", "description": "Server-side hint, e.g. 'stopped_idle' when the reaper paused this pod"},
                    },
                },
            },
        }),
    },
    {
        "path": "/pods/create",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Create a RunPod pod (async)",
        "description": (
            "Provision a new RunPod GPU pod or reuse an existing one. "
            "The pod automatically joins the Tailscale tailnet. "
            "Returns a job_id — poll GET /job/{job_id} to track progress."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "description": "Pod name (used as Tailscale hostname: comfy-{name})"},
                            "gpu_type": {"type": "string", "description": "GPU type (e.g. 'NVIDIA L40S'). Default from config."},
                            "image": {"type": "string", "description": "Docker image. Default from config."},
                            "volume_id": {"type": "string", "description": "Attach an existing network volume by ID"},
                            "volume_size_gb": {"type": "integer", "description": "Create a pod-local volume of this size (GB)"},
                            "datacenter": {"type": "string", "description": "Datacenter ID (e.g. 'US-KS-2')"},
                            "cloud_type": {"type": "string", "enum": ["SECURE", "COMMUNITY", "ALL"]},
                            "gpu_count": {"type": "integer", "default": 1},
                            "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Extra environment variables"},
                            "wait_ready": {"type": "boolean", "default": True, "description": "Wait for the pod's comfy-runner server to be reachable"},
                            "purpose": {
                                "type": "string",
                                "enum": ["pr", "persistent", "test"],
                                "default": "persistent",
                                "description": (
                                    "Recorded purpose tag for the pod. Defaults to ``persistent``. "
                                    "Use ``pr`` for review pods (also stamped automatically by /pods/launch-pr) "
                                    "or ``test`` for ephemeral test-runner pods (stamped automatically by the test runner). "
                                    "Any other value is rejected with HTTP 400."
                                ),
                            },
                        },
                    }
                }
            },
        },
        "responses": _async_response("Pod creation started"),
    },
    {
        "path": "/pods/{name}/deploy",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Deploy to a pod (async)",
        "description": (
            "Deploy a PR, branch, tag, or commit to a named RunPod pod. "
            "The central server connects to the pod's comfy-runner server via Tailscale "
            "and proxies the deploy operation. Returns a job_id."
        ),
        "parameters": [_POD_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "pr": {"type": "integer", "description": "PR number to deploy"},
                            "branch": {"type": "string", "description": "Branch name to checkout"},
                            "tag": {"type": "string", "description": "Tag to checkout"},
                            "commit": {"type": "string", "description": "Commit SHA to checkout"},
                            "reset": {"type": "boolean", "description": "Reset to original release ref"},
                            "latest": {"type": "boolean", "description": "Update to latest release"},
                            "pull": {"type": "boolean", "description": "Re-fetch current PR/branch"},
                            "install": {"type": "string", "default": "main", "description": "Installation name on pod"},
                            "start": {"type": "boolean", "default": True, "description": "Start ComfyUI after deploy"},
                            "repo": {"type": "string", "description": "GitHub repo URL for PR deploy"},
                            "title": {"type": "string", "description": "PR title for display"},
                            "launch_args": {"type": "string", "description": "ComfyUI launch arguments"},
                            "cuda_compat": {"type": "boolean", "description": "Auto-detect CUDA compatibility"},
                            "build": {"type": "boolean", "description": "Build standalone env instead of downloading"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Deploy started"),
    },
    {
        "path": "/pods/{name}/review",
        "method": "post",
        "tags": ["Pods", "Review"],
        "summary": "Prepare a PR for review on a pod (async)",
        "description": (
            "End-to-end PR review preparation on an existing pod. Auto-wakes "
            "the pod if it is stopped, deploys the requested PR via the pod's "
            "sidecar, and runs prepare_local_review (manifest + workflows + "
            "model downloads) server-side. The single returned job_id covers "
            "the entire flow; its result carries both deploy_result and "
            "review_result plus pod metadata."
        ),
        "parameters": [_POD_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["owner", "repo", "pr"],
                        "properties": {
                            "owner": {"type": "string", "description": "GitHub repo owner"},
                            "repo": {"type": "string", "description": "GitHub repo name"},
                            "pr": {"type": "integer", "description": "PR number"},
                            "install": {"type": "string", "default": "main", "description": "Installation name on the pod"},
                            "github_token": {"type": "string", "description": "GitHub token for fetching PR body (defaults to pod's $GITHUB_TOKEN)"},
                            "download_token": {"type": "string", "description": "Bearer token for authenticated model downloads"},
                            "extra_models": {
                                "type": "array",
                                "description": "Extra ModelEntry objects to merge into the manifest",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "url": {"type": "string"},
                                        "directory": {"type": "string"},
                                    },
                                },
                            },
                            "extra_workflows": {
                                "type": "array",
                                "description": "Extra workflow URLs to fetch",
                                "items": {"type": "string"},
                            },
                            "allow_arbitrary_urls": {"type": "boolean", "description": "Allow non-allowlisted workflow URL hosts"},
                            "skip_provisioning": {"type": "boolean", "description": "Skip model downloads (manifest + workflows only)"},
                            "title": {"type": "string", "description": "PR title for display in deploy step"},
                            "launch_args": {"type": "string", "description": "ComfyUI launch arguments for deploy step"},
                            "cuda_compat": {"type": "boolean", "description": "Auto-detect CUDA compatibility for deploy step"},
                            "force_purpose": {"type": "boolean", "description": "Override the refusal to review against pods tagged purpose='test' (e2e test pods)"},
                            "skip_deploy": {"type": "boolean", "description": "Skip the deploy step (use when the caller has already deployed, e.g. via POST /pods/launch-pr)"},
                            "force_deploy": {"type": "boolean", "description": "Always deploy even if the pod's installation already has this PR deployed (default is idempotent: skip deploy when current)"},
                            "idle_timeout_s": {"type": "integer", "description": "Per-review override for the pod's idle timeout (seconds). After review-prep finishes, the pod record is updated so the idle reaper uses the new value."},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Review preparation started"),
    },
    {
        "path": "/reviews/local",
        "method": "post",
        "tags": ["Review"],
        "summary": "Prepare a PR for review on this installation (async)",
        "description": (
            "Sidecar-side endpoint that runs prepare_local_review against a "
            "named installation: fetches the PR's comfyrunner manifest block "
            "from GitHub, downloads any declared workflow URLs into "
            "user/default/workflows/, and downloads missing models. The "
            "deploy step is *not* run here — callers are expected to deploy "
            "first via POST /<install>/deploy. Returns a job_id; its result "
            "is the same dict shape as comfy_runner.review.prepare_local_review."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["install", "owner", "repo", "pr"],
                        "properties": {
                            "install": {"type": "string", "description": "Installation name (must be a safe identifier)"},
                            "owner": {"type": "string", "description": "GitHub repo owner"},
                            "repo": {"type": "string", "description": "GitHub repo name"},
                            "pr": {"type": "integer", "description": "PR number"},
                            "github_token": {"type": "string", "description": "GitHub token for fetching PR body"},
                            "download_token": {"type": "string", "description": "Bearer token for authenticated model downloads"},
                            "extra_models": {
                                "type": "array",
                                "description": "Extra ModelEntry objects to merge into the manifest",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "url": {"type": "string"},
                                        "directory": {"type": "string"},
                                    },
                                },
                            },
                            "extra_workflows": {
                                "type": "array",
                                "description": "Extra workflow URLs to fetch",
                                "items": {"type": "string"},
                            },
                            "allow_arbitrary_urls": {"type": "boolean", "description": "Allow non-allowlisted workflow URL hosts"},
                            "skip_provisioning": {"type": "boolean", "description": "Skip model downloads (manifest + workflows only)"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Review preparation started"),
    },
    {
        "path": "/reviews/cleanup",
        "method": "post",
        "tags": ["Review", "Pods"],
        "summary": "Terminate ephemeral PR pods for a given PR",
        "description": (
            "Walks all pod records and terminates any whose record has "
            "``purpose == 'pr'`` AND ``pr_number == <pr>``. ``persistent`` "
            "and ``test`` pods are never touched. Synchronous: returns "
            "the per-pod termination outcome immediately."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["pr"],
                        "properties": {
                            "pr": {"type": "integer", "description": "PR number to clean up"},
                            "dry_run": {"type": "boolean", "description": "List matches without terminating"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Cleanup complete", {
            "pr": {"type": "integer"},
            "dry_run": {"type": "boolean"},
            "terminated": {"type": "array", "items": {"type": "object"}},
            "skipped": {"type": "array", "items": {"type": "object"}},
            "removed_records": {"type": "array", "items": {"type": "string"}},
            "total_found": {"type": "integer"},
            "total_terminated": {"type": "integer"},
        }),
    },
    {
        "path": "/pods/{name}/stop",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Stop a pod",
        "description": "Stop a running pod (preserves data, can be restarted later).",
        "parameters": [_POD_NAME_PARAM],
        "responses": _ok_response("Pod stopped", {
            "name": {"type": "string"},
            "action": {"type": "string", "enum": ["stopped"]},
        }),
    },
    {
        "path": "/pods/{name}/start",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Start a stopped pod (async)",
        "description": (
            "Start a previously stopped pod and optionally wait for the comfy-runner server to be ready. "
            "Returns a job_id."
        ),
        "parameters": [_POD_NAME_PARAM],
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "wait_ready": {"type": "boolean", "default": True, "description": "Wait for server readiness"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("Pod start initiated"),
    },
    {
        "path": "/pods/{name}",
        "method": "delete",
        "tags": ["Pods"],
        "summary": "Terminate a pod",
        "description": "Permanently terminate a pod and remove it from config.",
        "parameters": [_POD_NAME_PARAM],
        "responses": _ok_response("Pod terminated", {
            "name": {"type": "string"},
            "action": {"type": "string", "enum": ["terminated"]},
        }),
    },

    {
        "path": "/pods/launch-pr",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Launch a pod for a PR (async)",
        "description": (
            "Atomically create-or-wake a pod for reviewing a GitHub PR and deploy the PR to it. "
            "The pod is named ``pr-<repo-slug>-<num>`` (or ``pr-<num>`` if no repo is given). "
            "If a record already exists, an EXITED/STOPPED pod is started; a RUNNING pod is reused; "
            "a missing pod is recreated. The record is tagged ``purpose='pr'`` and is subject to "
            "the idle reaper, which stops the pod after ``idle_timeout_s`` seconds (default 600) "
            "of inactivity. Wake by calling this endpoint, /pods/{name}/start, /pods/{name}/touch, "
            "or any /pods/{name}/* operation. Returns a job_id."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["pr"],
                        "properties": {
                            "pr": {"type": "integer", "description": "GitHub PR number"},
                            "repo": {"type": "string", "description": "GitHub repo (URL or 'owner/name'), used both to slug the pod name and to pass to deploy"},
                            "gpu_type": {"type": "string"},
                            "image": {"type": "string"},
                            "volume_id": {"type": "string"},
                            "volume_size_gb": {"type": "integer"},
                            "datacenter": {"type": "string"},
                            "cloud_type": {"type": "string", "enum": ["SECURE", "COMMUNITY", "ALL"]},
                            "gpu_count": {"type": "integer", "default": 1},
                            "env": {"type": "object", "additionalProperties": {"type": "string"}},
                            "install": {"type": "string", "default": "main"},
                            "title": {"type": "string", "description": "PR title for display"},
                            "launch_args": {"type": "string"},
                            "idle_timeout_s": {"type": "integer", "default": 600, "description": "Seconds of inactivity before the idle reaper stops this pod"},
                        },
                    }
                }
            },
        },
        "responses": _async_response("PR launch started"),
    },
    {
        "path": "/pods/{name}/touch",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Reset the idle timer on a pod",
        "description": (
            "Mark a pod as active by stamping ``last_active_at = now`` on its record. "
            "This defers the idle reaper. Use whenever a client expects to keep using "
            "the pod but does not call any other tracked endpoint."
        ),
        "parameters": [_POD_NAME_PARAM],
        "responses": _ok_response("Activity recorded", {
            "name": {"type": "string"},
            "last_active_at": {"type": "integer"},
            "idle_in_s": {"type": "integer", "nullable": True},
        }),
    },
    {
        "path": "/pods/cleanup",
        "method": "post",
        "tags": ["Pods"],
        "summary": "Terminate orphaned test pods",
        "description": (
            "Find and terminate RunPod pods matching a name prefix (default: 'test-'). "
            "Useful for cleaning up orphaned ephemeral test pods. "
            "Use dry_run to preview without terminating."
        ),
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "prefix": {"type": "string", "default": "test-", "description": "Pod name prefix to match"},
                            "dry_run": {"type": "boolean", "default": False, "description": "List matching pods without terminating"},
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Cleanup result", {
            "prefix": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "total_found": {"type": "integer"},
            "total_terminated": {"type": "integer"},
            "terminated": {"type": "array", "items": {"type": "object"}},
            "skipped": {"type": "array", "items": {"type": "object"}},
            "removed_records": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pod names whose registry records were also removed.",
            },
        }),
    },

    # ── Tests (Central Orchestration) ────────────────────────────────
    {
        "path": "/tests/run",
        "method": "post",
        "tags": ["Tests"],
        "summary": "Run a test suite against a target (async)",
        "description": (
            "Run a test suite against a single target (local ComfyUI URL, remote pod, or ephemeral RunPod). "
            "Returns a job_id — poll GET /tests/{test_id} to track progress."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["suite", "target"],
                        "properties": {
                            "suite": {"type": "string", "description": "Suite name (resolved from server's managed suites directory) or filesystem path"},
                            "target": {
                                "type": "object",
                                "description": "Target to test against",
                                "properties": {
                                    "kind": {"type": "string", "enum": ["local", "remote", "runpod"]},
                                    "url": {"type": "string", "description": "ComfyUI URL (local target)"},
                                    "pod_name": {"type": "string", "description": "Pod name (remote target)"},
                                    "server_url": {"type": "string", "description": "Explicit server URL (remote target)"},
                                    "gpu_type": {"type": "string", "description": "GPU type (runpod target)"},
                                    "install": {"type": "string", "default": "main"},
                                    "label": {"type": "string", "description": "Human-readable label"},
                                },
                            },
                            "timeout": {"type": "integer", "default": 600, "description": "Per-workflow timeout (seconds)"},
                            "formats": {"type": "string", "default": "json,html,markdown"},
                            "max_runtime_s": {
                                "type": "integer",
                                "description": (
                                    "Suite-level wall-clock budget. Overrides the suite.json value "
                                    "for this run. When exceeded the watchdog aborts the run, calls "
                                    "ComfyUI's POST /interrupt, writes a synthetic ``overrun`` "
                                    "failure row, and dispatches ``on_overrun``."
                                ),
                            },
                            "on_overrun": {
                                "type": "string",
                                "enum": ["none", "stop", "terminate"],
                                "description": (
                                    "Pod action to take when the watchdog aborts the run. "
                                    "Defaults to ``terminate`` for runpod targets, ``stop`` for "
                                    "remote targets, and ``none`` for local targets. "
                                    "``stop`` falls back to ``terminate`` for untracked pods."
                                ),
                            },
                        },
                    }
                }
            },
        },
        "responses": _async_response("Test run started"),
    },
    {
        "path": "/tests/fleet",
        "method": "post",
        "tags": ["Tests"],
        "summary": "Run a test suite across multiple targets (async)",
        "description": (
            "Execute a test suite in parallel across a fleet of targets. "
            "Each target gets its own subdirectory and report. "
            "Returns a job_id."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["suite", "targets"],
                        "properties": {
                            "suite": {"type": "string", "description": "Suite name (resolved from server's managed suites directory) or filesystem path"},
                            "targets": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"type": "string", "enum": ["local", "remote", "runpod"]},
                                        "url": {"type": "string"},
                                        "pod_name": {"type": "string"},
                                        "server_url": {"type": "string"},
                                        "gpu_type": {"type": "string"},
                                        "install": {"type": "string", "default": "main"},
                                        "label": {"type": "string"},
                                    },
                                },
                                "description": "List of targets to test against in parallel",
                            },
                            "timeout": {"type": "integer", "default": 600},
                            "max_workers": {"type": "integer", "description": "Max parallel workers (default: min(targets, 4))"},
                            "formats": {"type": "string", "default": "json,html,markdown"},
                            "max_runtime_s": {
                                "type": "integer",
                                "description": (
                                    "Fleet-level wall-clock budget (seconds). Overrides the "
                                    "suite.json value. When exceeded, the watchdog cancels the "
                                    "fleet, dispatches ``on_overrun`` per target, and the run is "
                                    "marked ``timed_out``."
                                ),
                            },
                            "on_overrun": {
                                "type": "string",
                                "enum": ["none", "stop", "terminate"],
                                "description": (
                                    "Pod action to take per target when the fleet watchdog aborts. "
                                    "Defaults per target kind: ``terminate`` for runpod, ``stop`` "
                                    "for remote, ``none`` for local."
                                ),
                            },
                        },
                    }
                }
            },
        },
        "responses": _async_response("Fleet test started"),
    },
    {
        "path": "/tests",
        "method": "get",
        "tags": ["Tests"],
        "summary": "List recent test runs",
        "description": "Returns recent test runs (newest first) with current status from the job tracker.",
        "parameters": [
            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50}, "description": "Max runs to return"},
        ],
        "responses": _ok_response("Test run list", {
            "runs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["single", "fleet"]},
                        "suite": {"type": "string"},
                        "status": {"type": "string"},
                        "targets": {"type": "array", "items": {"type": "object"}},
                        "created_at": {"type": "number"},
                        "finished_at": {"type": "number", "nullable": True},
                        "summary": {"type": "object", "nullable": True},
                    },
                },
            },
        }),
    },
    {
        "path": "/tests/{test_id}",
        "method": "get",
        "tags": ["Tests"],
        "summary": "Get test run status",
        "description": (
            "Returns detailed status for a test run including job output lines, "
            "test metadata, and results when complete."
        ),
        "parameters": [_TEST_ID_PARAM],
        "responses": _ok_response("Test run details", {
            "id": {"type": "string"},
            "kind": {"type": "string"},
            "suite": {"type": "string"},
            "status": {
                "type": "string",
                "description": (
                    "Run state. ``timed_out`` indicates the watchdog aborted the run "
                    "because ``max_runtime_s`` was exceeded."
                ),
            },
            "targets": {"type": "array", "items": {"type": "object"}},
            "output": {"type": "array", "items": {"type": "string"}},
            "result": {"type": "object", "nullable": True},
            "summary": {
                "type": "object",
                "nullable": True,
                "description": (
                    "Aggregate run summary. Includes ``timed_out`` (bool) and "
                    "``aborted_reason`` (e.g. ``\"overrun\"``) when the watchdog fired, "
                    "plus ``on_overrun_action(s)`` describing the pod cleanup that "
                    "followed."
                ),
            },
            "timed_out": {"type": "boolean"},
        }),
    },
    {
        "path": "/tests/{test_id}/report",
        "method": "get",
        "tags": ["Tests"],
        "summary": "Get test report",
        "description": (
            "Retrieve the test report in JSON, HTML, or Markdown format. "
            "For fleet runs, returns the fleet summary report."
        ),
        "parameters": [
            _TEST_ID_PARAM,
            {"name": "format", "in": "query", "schema": {"type": "string", "default": "json", "enum": ["json", "html", "markdown"]}, "description": "Report format"},
        ],
        "responses": _ok_response("Test report", {
            "test_id": {"type": "string"},
            "report": {"type": "object", "description": "Report data (JSON format only)"},
        }),
    },

    # ── Dashboard ─────────────────────────────────────────────────────
    {
        "path": "/dashboard",
        "method": "get",
        "tags": ["Dashboard"],
        "summary": "HTML status dashboard",
        "description": (
            "Server-rendered HTML page showing active pods, running tests, and recent results. "
            "Auto-refreshes every 15 seconds."
        ),
        "responses": {
            "200": {
                "description": "HTML dashboard page",
                "content": {"text/html": {"schema": {"type": "string"}}},
            },
        },
    },

    # ── Suites ────────────────────────────────────────────────────────
    {
        "path": "/suites",
        "method": "get",
        "tags": ["Suites"],
        "summary": "List available test suites",
        "description": "Returns all test suites on the server with metadata and run counts.",
        "responses": _ok_response("Suite list", {
            "suites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "required_models": {"type": "array", "items": {"type": "string"}},
                        "workflow_count": {"type": "integer"},
                        "run_count": {"type": "integer"},
                    },
                },
            },
        }),
    },
    {
        "path": "/suites/{name}",
        "method": "get",
        "tags": ["Suites"],
        "summary": "Get suite details",
        "description": "Returns suite metadata, config, workflow filenames, and run IDs.",
        "parameters": [_SUITE_NAME_PARAM],
        "responses": _ok_response("Suite details", {
            "name": {"type": "string"},
            "suite": {"type": "object"},
            "config": {"type": "object"},
            "workflows": {"type": "array", "items": {"type": "string"}},
            "runs": {"type": "array", "items": {"type": "string"}},
        }),
    },
    {
        "path": "/suites/{name}",
        "method": "post",
        "tags": ["Suites"],
        "summary": "Upload or update a test suite",
        "description": (
            "Upload suite definition files. Preserves any existing test runs. "
            "Replaces all workflow files."
        ),
        "parameters": [_SUITE_NAME_PARAM],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["suite", "workflows"],
                        "properties": {
                            "suite": {"type": "object", "description": "suite.json contents"},
                            "config": {"type": "object", "description": "config.json contents"},
                            "workflows": {
                                "type": "object",
                                "additionalProperties": {"type": "object"},
                                "description": "Map of filename → workflow JSON",
                            },
                        },
                    }
                }
            },
        },
        "responses": _ok_response("Suite uploaded", {
            "name": {"type": "string"},
            "workflows": {"type": "array", "items": {"type": "string"}},
            "message": {"type": "string"},
        }),
    },
    {
        "path": "/suites/{name}",
        "method": "delete",
        "tags": ["Suites"],
        "summary": "Remove a suite from the server",
        "description": (
            "Removes suite definition files. By default refuses if test runs exist "
            "(use ?force=true). Runs are preserved unless ?include_runs=true."
        ),
        "parameters": [
            _SUITE_NAME_PARAM,
            {"name": "force", "in": "query", "schema": {"type": "boolean", "default": False}, "description": "Force deletion even if test runs exist"},
            {"name": "include_runs", "in": "query", "schema": {"type": "boolean", "default": False}, "description": "Also delete test run data"},
        ],
        "responses": _ok_response("Suite removed", {
            "name": {"type": "string"},
            "action": {"type": "string"},
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
