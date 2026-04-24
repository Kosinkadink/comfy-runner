"""HTTP control API for remote management.

Thin wrapper over existing CLI modules. Each endpoint delegates to the same
functions that cli.py uses. All responses use the --json format:
  {"ok": true, ...} on success
  {"ok": false, "error": "..."} on failure

Routes are prefixed with /<name>/ where name is the installation name.
Top-level routes (e.g. GET /installations) operate across all installations.

Long-running operations (deploy, restart, snapshot restore, node add/rm)
run in background threads and return immediately with a job_id.  Poll
GET /job/<job_id> to check status and retrieve results.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("comfy-runner-server")

_tailscale_mode = False
_tunnels_enabled = False


def set_tailscale_mode(enabled: bool) -> None:
    global _tailscale_mode
    _tailscale_mode = enabled


def set_tunnels_enabled(enabled: bool) -> None:
    global _tunnels_enabled
    _tunnels_enabled = enabled


# ---------------------------------------------------------------------------
# Output collector — captures send_output calls into a list
# ---------------------------------------------------------------------------

def _make_collector(
    job_id: str | None = None,
) -> tuple[Callable[[str], None], list[str]]:
    """Return (send_output, lines) — thread-safe output collector.

    If *job_id* is given, output is written directly to the job's
    ``output`` list so callers can poll progress in real time.
    """
    if job_id is not None:
        job = _jobs.get(job_id)
        if job:
            lines = job["output"]
        else:
            lines = []
    else:
        lines = []
    lock = threading.Lock()

    def collect(text: str) -> None:
        with lock:
            if text.startswith("\r") and lines:
                lines[-1] = text
            else:
                lines.append(text)

    return collect, lines


def _err(msg: str, status: int = 400) -> tuple[Any, int]:
    from flask import jsonify
    return jsonify({"ok": False, "error": msg}), status


def _get_record(name: str) -> tuple[dict | None, str]:
    """Get an installation record, returning (record, error_msg)."""
    from comfy_runner.config import get_installation
    record = get_installation(name)
    if not record:
        return None, f"Installation '{name}' not found."
    return record, ""


# ---------------------------------------------------------------------------
# Background job tracker
# ---------------------------------------------------------------------------

class _JobTracker:
    """Thread-safe store for background jobs.

    Jobs expire after ``ttl`` seconds once completed.
    """

    def __init__(self, ttl: int = 600) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._ttl = ttl

    def create(self, label: str = "") -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {
                "id": job_id,
                "label": label,
                "status": "running",
                "result": None,
                "error": None,
                "output": [],
                "started_at": time.time(),
                "finished_at": None,
            }
            self._cancel_events[job_id] = threading.Event()
        log.info("[job %s] started: %s", job_id, label)
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Signal cancellation for a running job. Returns True if the job was found."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "running":
                return False
            job["status"] = "cancelled"
            job["finished_at"] = time.time()
            elapsed = job["finished_at"] - job["started_at"]
            log.info("[job %s] cancelled: %s (%.1fs)", job_id, job["label"], elapsed)
        evt = self._cancel_events.get(job_id)
        if evt:
            evt.set()
        return True

    def get_cancel_event(self, job_id: str) -> threading.Event | None:
        """Return the cancellation Event for a job, or None."""
        with self._lock:
            return self._cancel_events.get(job_id)

    def finish(self, job_id: str, result: dict[str, Any], output: list[str]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "done"
                job["result"] = result
                job["output"] = output
                job["finished_at"] = time.time()
                elapsed = job["finished_at"] - job["started_at"]
                log.info("[job %s] done: %s (%.1fs)", job_id, job["label"], elapsed)

    def fail(self, job_id: str, error: str, output: list[str]) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["error"] = error
                job["output"] = output
                job["finished_at"] = time.time()
                elapsed = job["finished_at"] - job["started_at"]
                log.error("[job %s] FAILED: %s — %s (%.1fs)", job_id, job["label"], error, elapsed)
                if output:
                    # Log last few output lines for context
                    for line in output[-5:]:
                        log.error("[job %s]   %s", job_id, line.rstrip())

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._gc()
            return self._jobs.get(job_id)

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock:
            self._gc()
            return [
                {k: v for k, v in j.items() if k != "output"}
                for j in self._jobs.values()
            ]

    def _gc(self) -> None:
        """Remove expired completed jobs (caller must hold lock)."""
        now = time.time()
        expired = [
            jid for jid, j in self._jobs.items()
            if j["finished_at"] and now - j["finished_at"] > self._ttl
        ]
        for jid in expired:
            del self._jobs[jid]
            self._cancel_events.pop(jid, None)


_jobs = _JobTracker()


def _capture_and_track(
    name: str,
    install_path: str,
    trigger: str,
    out: Callable[[str], None] | None = None,
    label: str | None = None,
) -> str | None:
    """Capture a snapshot and update last_snapshot/snapshot_count on the record.

    Mirrors Desktop 2.0's pattern: every boot, restart, deploy, and restore
    captures a snapshot and stores the filename on the installation record.
    Returns the saved filename, or None if unchanged.
    """
    from comfy_runner.config import get_installation, set_installation
    from comfy_runner.snapshot import capture_snapshot_if_changed, get_snapshot_count

    rec = get_installation(name)
    if not rec:
        return None

    last = rec.get("last_snapshot")
    try:
        result = capture_snapshot_if_changed(
            install_path, trigger=trigger, last_snapshot=last,
        )
        if result.get("saved") and result.get("filename"):
            filename = result["filename"]
            rec = get_installation(name) or rec
            rec["last_snapshot"] = filename
            rec["snapshot_count"] = get_snapshot_count(install_path)
            set_installation(name, rec)
            if out:
                out(f"Snapshot saved: {filename} (trigger: {trigger})\n")
            return filename
    except Exception as e:
        if out:
            out(f"Snapshot capture failed: {e}\n")
    return None


# Per-installation locks — prevent concurrent operations on the same install
_install_locks: dict[str, threading.Lock] = {}
_install_locks_guard = threading.Lock()


def _get_install_lock(name: str) -> threading.Lock:
    """Return a per-installation lock, creating one if needed."""
    with _install_locks_guard:
        if name not in _install_locks:
            _install_locks[name] = threading.Lock()
        return _install_locks[name]


# ---------------------------------------------------------------------------
# Flask app factory
# ---------------------------------------------------------------------------

def create_app() -> Any:
    """Create and return a Flask app that manages all installations."""
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.after_request
    def _log_request(response: Any) -> Any:
        if request.method != "GET":
            log.info("%s %s → %s", request.method, request.path, response.status_code)
        else:
            log.debug("%s %s → %s", request.method, request.path, response.status_code)
        return response

    # ------------------------------------------------------------------
    # GET /openapi.json — auto-generated OpenAPI spec
    # ------------------------------------------------------------------
    @app.route("/openapi.json", methods=["GET"])
    def route_openapi() -> Any:
        from comfy_runner_server.openapi import build_spec
        return jsonify(build_spec())

    # Disable Flask's default HTML error pages — return JSON always
    @app.errorhandler(404)
    def not_found(_e: Any) -> Any:
        return jsonify({"ok": False, "error": "Not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_e: Any) -> Any:
        return jsonify({"ok": False, "error": "Method not allowed"}), 405

    @app.errorhandler(500)
    def internal_error(_e: Any) -> Any:
        return jsonify({"ok": False, "error": "Internal server error"}), 500

    # ------------------------------------------------------------------
    # GET /jobs — list active/recent jobs
    # ------------------------------------------------------------------
    @app.route("/jobs", methods=["GET"])
    def route_jobs() -> Any:
        return jsonify({"ok": True, "jobs": _jobs.list_active()})

    # ------------------------------------------------------------------
    # GET /job/<job_id> — poll a background job
    # ------------------------------------------------------------------
    @app.route("/job/<job_id>", methods=["GET"])
    def route_job(job_id: str) -> Any:
        job = _jobs.get(job_id)
        if not job:
            return _err("Job not found", 404)
        return jsonify({"ok": True, **job})

    # ------------------------------------------------------------------
    # POST /job/<job_id>/cancel — cancel a running job
    # ------------------------------------------------------------------
    @app.route("/job/<job_id>/cancel", methods=["POST"])
    def route_job_cancel(job_id: str) -> Any:
        if _jobs.cancel(job_id):
            return jsonify({"ok": True})
        job = _jobs.get(job_id)
        if not job:
            return _err("Job not found", 404)
        return _err(f"Job is not running (status: {job['status']})")

    # ------------------------------------------------------------------
    # GET /installations — list all installations with status
    # ------------------------------------------------------------------
    @app.route("/installations", methods=["GET"])
    def route_installations() -> Any:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from comfy_runner.installations import show_list
        from comfy_runner.process import get_status
        from comfy_runner.tunnel import get_tunnel_url

        def _fetch_inst_status(inst: dict) -> None:
            try:
                status = get_status(inst["name"])
                if _tailscale_mode and status.get("port"):
                    from comfy_runner.tunnel import _load_serve_registry, get_tailscale_serve_url
                    if status["port"] in _load_serve_registry():
                        serve_url = get_tailscale_serve_url(status["port"])
                        if serve_url:
                            status["serve_url"] = serve_url
                tunnel_url = get_tunnel_url(inst["name"])
                if tunnel_url:
                    status["tunnel_url"] = tunnel_url
                inst["_status"] = status
            except Exception:
                inst["_status"] = {"running": False}

        try:
            installs = show_list()
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(_fetch_inst_status, inst) for inst in installs]
                for f in as_completed(futs):
                    f.result()  # propagate exceptions
            return jsonify({"ok": True, "installations": installs})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /system-info — hardware and system information
    # ------------------------------------------------------------------
    @app.route("/system-info", methods=["GET"])
    def route_system_info() -> Any:
        from comfy_runner.system_info import get_system_info
        try:
            info = get_system_info()
            return jsonify({"ok": True, "system_info": info})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /status — aggregate status (all installations)
    # ------------------------------------------------------------------
    @app.route("/status", methods=["GET"])
    def route_status_all() -> Any:
        from comfy_runner.installations import show_list
        from comfy_runner.process import get_status

        try:
            installs = show_list()
            results = []
            for inst in installs:
                try:
                    status = get_status(inst["name"])
                    status["name"] = inst["name"]
                    results.append(status)
                except Exception:
                    results.append({"name": inst["name"], "running": False})
            # For backwards compat: if any installation is running, report as running
            first_running = next((r for r in results if r.get("running")), None)
            resp: dict[str, Any] = {"ok": True, "installations": results}
            if first_running:
                resp.update(first_running)
            else:
                resp["running"] = False
            return jsonify(resp)
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/status
    # ------------------------------------------------------------------
    @app.route("/<name>/status", methods=["GET"])
    def route_status(name: str) -> Any:
        from comfy_runner.process import get_status
        from comfy_runner.tunnel import get_tunnel_url

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            status = get_status(name)
            # Tailscale serve URL (private tailnet access)
            if _tailscale_mode and status.get("port"):
                from comfy_runner.tunnel import _load_serve_registry, get_tailscale_serve_url
                if status["port"] in _load_serve_registry():
                    serve_url = get_tailscale_serve_url(status["port"])
                    if serve_url:
                        status["serve_url"] = serve_url
            # Tunnel URL (ngrok/funnel public access)
            tunnel_url = get_tunnel_url(name)
            if tunnel_url:
                status["tunnel_url"] = tunnel_url
            return jsonify({"ok": True, **status})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/info
    # ------------------------------------------------------------------
    @app.route("/<name>/info", methods=["GET"])
    def route_info(name: str) -> Any:
        from comfy_runner.process import get_status
        from comfy_runner.tunnel import get_tunnel_url

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            status = get_status(name)
            info: dict[str, Any] = {
                "ok": True,
                "name": name,
                "path": record.get("path"),
                "variant": record.get("variant"),
                "release_tag": record.get("release_tag"),
                "comfyui_ref": record.get("comfyui_ref"),
                "head_commit": record.get("head_commit"),
                "python_version": record.get("python_version"),
                "launch_args": record.get("launch_args"),
                "created_at": record.get("created_at"),
                "deployed_pr": record.get("deployed_pr"),
                "deployed_branch": record.get("deployed_branch"),
                "deployed_repo": record.get("deployed_repo"),
                "deployed_title": record.get("deployed_title"),
                "running": status.get("running", False),
                "pid": status.get("pid"),
                "port": status.get("port"),
                "healthy": status.get("healthy"),
            }
            if _tailscale_mode and status.get("port"):
                from comfy_runner.tunnel import _load_serve_registry, get_tailscale_serve_url
                if status["port"] in _load_serve_registry():
                    serve_url = get_tailscale_serve_url(status["port"])
                    if serve_url:
                        info["serve_url"] = serve_url
            tunnel_url = get_tunnel_url(name)
            if tunnel_url:
                info["tunnel_url"] = tunnel_url
            return jsonify(info)
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/deploy  (async — returns job_id)
    # ------------------------------------------------------------------
    @app.route("/<name>/deploy", methods=["POST"])
    def route_deploy(name: str) -> Any:
        record, _ = _get_record(name)

        body = request.get_json(silent=True) or {}

        # Validate before spawning thread
        pr = body.get("pr")
        branch = body.get("branch")
        tag = body.get("tag")
        commit = body.get("commit")
        reset = body.get("reset", False)
        latest = body.get("latest", False)
        pull = body.get("pull", False)
        modes = [pr is not None, bool(branch), bool(tag), bool(commit), reset, latest, pull]
        if sum(modes) != 1:
            return _err("Specify exactly one of: pr, branch, tag, commit, reset, latest, or pull")

        needs_init = record is None
        job_id = _jobs.create(label=f"{'init + ' if needs_init else ''}deploy {name}")

        def _run() -> None:
            from comfy_runner.config import get_installation, set_installation
            from comfy_runner.deployments import execute_deploy
            from comfy_runner.installations import init_installation
            from comfy_runner.pip_utils import install_filtered_requirements
            from comfy_runner.process import get_status, start_installation, stop_installation

            out, lines = _make_collector(job_id)
            lock = _get_install_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                return
            try:
                # Auto-init if the installation doesn't exist yet
                rec = get_installation(name)
                if not rec:
                    out(f"Installation '{name}' not found — initializing...\n")
                    try:
                        cuda_compat = body.get("cuda_compat", False)
                        from comfy_runner.hosted.config import get_provider_config
                        prov_cfg = get_provider_config("runpod")
                        cache_releases = prov_cfg.get("cache_releases")
                        cache_kw: dict = {}
                        if isinstance(cache_releases, int):
                            cache_kw["max_cache_entries"] = cache_releases
                        build_kw: dict = {}
                        if body.get("build"):
                            build_kw["build"] = True
                            for bk in ("python_version", "pbs_release", "gpu",
                                       "cuda_tag", "torch_version", "torch_spec",
                                       "torch_index_url", "comfyui_ref"):
                                if bk in body:
                                    build_kw[bk] = body[bk]
                        init_installation(
                            name=name, send_output=out, cuda_compat=cuda_compat,
                            **cache_kw, **build_kw,
                        )
                    except Exception as e:
                        _jobs.fail(job_id, f"Auto-init failed: {e}", lines)
                        return
                    rec = get_installation(name)
                    if not rec:
                        _jobs.fail(job_id, "Installation record missing after init", lines)
                        return
                    existing_args = rec.get("launch_args", "") or ""
                    if "--listen" not in existing_args:
                        rec["launch_args"] = f"--listen 0.0.0.0 {existing_args}".strip()
                        set_installation(name, rec)
                    out(f"\n{'='*50}\nProceeding with deploy...\n{'='*50}\n\n")

                install_path = rec["path"]

                status = get_status(name)
                was_running = status.get("running", False)
                running_port = status.get("port")

                if was_running:
                    try:
                        stop_installation(name, send_output=out)
                    except RuntimeError:
                        pass

                result, updates = execute_deploy(
                    install_path, rec,
                    pr=int(pr) if pr else None,
                    branch=branch,
                    tag=tag,
                    commit=commit,
                    reset=reset,
                    latest=latest,
                    pull=pull,
                    repo_url=body.get("repo"),
                    send_output=out,
                )

                # Install requirements if changed
                changed_files = result.get("changed_files", [])
                req_changed = any(
                    f in ("requirements.txt", "manager_requirements.txt")
                    for f in changed_files
                )
                if req_changed:
                    req_path = Path(install_path) / "ComfyUI" / "requirements.txt"
                    rc = install_filtered_requirements(
                        install_path, req_path, send_output=out
                    )
                    result["requirements_installed"] = rc == 0
                else:
                    result["requirements_installed"] = False

                # Apply record updates from shared helper
                for k, v in updates.items():
                    if v is None:
                        rec.pop(k, None)
                    else:
                        rec[k] = v
                # Preserve repo/title from request body for PR deploys
                if pr:
                    rec["deployed_pr"] = int(pr)
                    rec["deployed_repo"] = body.get("repo", "")
                    rec["deployed_title"] = body.get("title", "")
                if "launch_args" in body:
                    rec["launch_args"] = body["launch_args"]
                set_installation(name, rec)

                should_start = was_running or body.get("start", False)
                if should_start:
                    start_result = start_installation(
                        name, port_override=running_port, send_output=out
                    )
                    result["restarted"] = True
                    result["port"] = start_result.get("port")
                    result["pid"] = start_result.get("pid")
                    if _tailscale_mode and start_result.get("port"):
                        try:
                            from comfy_runner.tunnel import start_tailscale_serve_port
                            ts_url = start_tailscale_serve_port(start_result["port"], send_output=out)
                            result["tailscale_url"] = ts_url
                        except Exception as e:
                            out(f"⚠ Tailscale serve failed: {e}\n")
                else:
                    result["restarted"] = False

                _capture_and_track(
                    name, install_path, "post-update", out=out,
                )
                _jobs.finish(job_id, result, lines)

            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # POST /<name>/restart  (async — returns job_id)
    # ------------------------------------------------------------------
    @app.route("/<name>/restart", methods=["POST"])
    def route_restart(name: str) -> Any:
        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        extra_args = body.get("extra_args")

        job_id = _jobs.create(label=f"restart {name}")

        def _run() -> None:
            from comfy_runner.config import get_installation
            from comfy_runner.process import get_status, start_installation, stop_installation

            out, lines = _make_collector(job_id)
            lock = _get_install_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                return
            try:
                rec = get_installation(name)
                install_path = rec["path"] if rec else ""

                status = get_status(name)
                running_port = status.get("port")

                try:
                    stop_installation(name, send_output=out)
                except RuntimeError:
                    pass

                result = start_installation(name, port_override=running_port, extra_args=extra_args, send_output=out)
                if _tailscale_mode and result.get("port"):
                    try:
                        from comfy_runner.tunnel import start_tailscale_serve_port
                        ts_url = start_tailscale_serve_port(result["port"], send_output=out)
                        result["tailscale_url"] = ts_url
                    except Exception as e:
                        out(f"⚠ Tailscale serve failed: {e}\n")
                if install_path:
                    _capture_and_track(name, install_path, "restart", out=out)
                _jobs.finish(job_id, result, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # POST /<name>/stop
    # ------------------------------------------------------------------
    @app.route("/<name>/stop", methods=["POST"])
    def route_stop(name: str) -> Any:
        from comfy_runner.process import get_status, stop_installation

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        out, lines = _make_collector()

        try:
            status = get_status(name)
            if not status.get("running"):
                return jsonify({"ok": True, "was_running": False, "output": lines})
            if _tailscale_mode and status.get("port"):
                try:
                    from comfy_runner.tunnel import stop_tailscale_serve_port
                    stop_tailscale_serve_port(status["port"], send_output=out)
                except Exception:
                    pass
            stop_installation(name, send_output=out)
            return jsonify({"ok": True, "was_running": True, "output": lines})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # DELETE /<name>
    # ------------------------------------------------------------------
    @app.route("/<name>", methods=["DELETE"])
    def route_remove(name: str) -> Any:
        from comfy_runner.config import remove_installation
        from comfy_runner.process import get_status, stop_installation

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        out, lines = _make_collector()

        try:
            # Stop tunnel, tailscale serve, and instance if running
            status = get_status(name)
            if status.get("running"):
                if status.get("port"):
                    from comfy_runner.tunnel import stop_tunnel
                    try:
                        stop_tunnel(name, send_output=out)
                    except Exception:
                        pass
                    from comfy_runner.lifecycle import maybe_tailscale_unserve
                    maybe_tailscale_unserve(status["port"], send_output=out)
                stop_installation(name, send_output=out)

            # Remove from config (does not delete files on disk)
            removed = remove_installation(name)
            return jsonify({"ok": True, "removed": removed, "output": lines})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # PUT /<name>/config — update installation config (e.g. launch_args)
    # ------------------------------------------------------------------
    @app.route("/<name>/config", methods=["PUT"])
    def route_config(name: str) -> Any:
        from comfy_runner.config import get_installation, set_installation

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        allowed_keys = {"launch_args"}
        updated = {}
        for key in allowed_keys:
            if key in body:
                record[key] = body[key]
                updated[key] = body[key]

        if not updated:
            return _err(f"No valid keys. Allowed: {', '.join(sorted(allowed_keys))}")

        set_installation(name, record)
        return jsonify({"ok": True, "updated": updated})

    # ------------------------------------------------------------------
    # POST /<name>/rename — rename an installation
    # ------------------------------------------------------------------
    @app.route("/<name>/rename", methods=["POST"])
    def route_rename(name: str) -> Any:
        from comfy_runner.config import rename_installation
        from comfy_runner.process import get_status

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        new_name = body.get("name")
        if not new_name or not isinstance(new_name, str):
            return _err("'name' is required (string)")

        status = get_status(name)
        if status.get("running"):
            return _err(f"Installation '{name}' is running — stop it first")

        try:
            updated = rename_installation(name, new_name)
            return jsonify({"ok": True, "old_name": name, "new_name": new_name})
        except ValueError as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/nodes
    # ------------------------------------------------------------------
    @app.route("/<name>/nodes", methods=["GET"])
    def route_nodes_list(name: str) -> Any:
        from comfy_runner.nodes import scan_custom_nodes

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            nodes = scan_custom_nodes(record["path"])
            return jsonify({"ok": True, "nodes": nodes})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/nodes  {"action": "add|rm|enable|disable", ...}
    #   add/rm are async (slow), enable/disable are sync (fast)
    # ------------------------------------------------------------------
    @app.route("/<name>/nodes", methods=["POST"])
    def route_nodes_action(name: str) -> Any:
        from comfy_runner.nodes import disable_node, enable_node

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        action = body.get("action", "")

        # Fast sync actions
        if action == "enable":
            node_name = body.get("node_name", "")
            if not node_name:
                return _err("Missing 'node_name' field")
            out, lines = _make_collector()
            try:
                enable_node(record["path"], node_name, send_output=out)
                return jsonify({"ok": True, "output": lines})
            except Exception as e:
                return _err(str(e))

        if action == "disable":
            node_name = body.get("node_name", "")
            if not node_name:
                return _err("Missing 'node_name' field")
            out, lines = _make_collector()
            try:
                disable_node(record["path"], node_name, send_output=out)
                return jsonify({"ok": True, "output": lines})
            except Exception as e:
                return _err(str(e))

        # Slow async actions (use per-installation lock)
        install_path = record["path"]

        if action == "add":
            source = body.get("source", "")
            if not source:
                return _err("Missing 'source' field")
            version = body.get("version")
            job_id = _jobs.create(label=f"node add {source}")

            def _run() -> None:
                from comfy_runner.nodes import add_cnr_node, add_git_node
                out, lines = _make_collector(job_id)
                lock = _get_install_lock(name)
                if not lock.acquire(timeout=5):
                    _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                    return
                try:
                    if source.startswith(("http://", "https://", "git@", "git://")):
                        node = add_git_node(install_path, source, send_output=out)
                    else:
                        node = add_cnr_node(install_path, source, version=version, send_output=out)
                    _jobs.finish(job_id, {"node": node}, lines)
                except Exception as e:
                    _jobs.fail(job_id, str(e), lines)
                finally:
                    lock.release()

            threading.Thread(target=_run, daemon=True).start()
            return jsonify({"ok": True, "job_id": job_id, "async": True})

        if action == "rm":
            node_name = body.get("node_name", "")
            if not node_name:
                return _err("Missing 'node_name' field")
            job_id = _jobs.create(label=f"node rm {node_name}")

            def _run() -> None:
                from comfy_runner.nodes import remove_node
                out, lines = _make_collector(job_id)
                lock = _get_install_lock(name)
                if not lock.acquire(timeout=5):
                    _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                    return
                try:
                    remove_node(install_path, node_name, send_output=out)
                    _jobs.finish(job_id, {}, lines)
                except Exception as e:
                    _jobs.fail(job_id, str(e), lines)
                finally:
                    lock.release()

            threading.Thread(target=_run, daemon=True).start()
            return jsonify({"ok": True, "job_id": job_id, "async": True})

        return _err(f"Unknown action: {action!r}. Use add|rm|enable|disable")

    # ------------------------------------------------------------------
    # GET /<name>/logs — current session log (or tail with ?lines=N)
    # ------------------------------------------------------------------
    @app.route("/<name>/logs", methods=["GET"])
    def route_logs(name: str) -> Any:
        from comfy_runner.log_utils import read_current_log, read_log_after

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        after = request.args.get("after", type=int)
        lines = request.args.get("lines", type=int)

        try:
            if after is not None:
                result = read_log_after(record["path"], after)
            else:
                result = read_current_log(record["path"], max_lines=lines)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/logs/sessions — list all log sessions
    # ------------------------------------------------------------------
    @app.route("/<name>/logs/sessions", methods=["GET"])
    def route_log_sessions(name: str) -> Any:
        from comfy_runner.log_utils import list_log_sessions

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            sessions = list_log_sessions(record["path"])
            return jsonify({"ok": True, "sessions": sessions})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/tunnel/start
    # ------------------------------------------------------------------
    @app.route("/<name>/tunnel/start", methods=["POST"])
    def route_tunnel_start(name: str) -> Any:
        if not _tunnels_enabled:
            return _err("Tunnels are not enabled on this server (start with --tunnels)")

        from comfy_runner.tunnel import start_tunnel

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        provider = body.get("provider", "tailscale")
        domain = body.get("domain", "")
        out, lines = _make_collector()

        log.info("Starting %s tunnel for '%s'…", provider, name)
        try:
            result = start_tunnel(name, provider=provider, send_output=out, domain=domain)
            log.info("Tunnel started for '%s': %s", name, result.get("url", "?"))
            return jsonify({"ok": True, **result, "output": lines})
        except Exception as e:
            log.error("Tunnel start failed for '%s': %s", name, e)
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/tunnel/stop
    # ------------------------------------------------------------------
    @app.route("/<name>/tunnel/stop", methods=["POST"])
    def route_tunnel_stop(name: str) -> Any:
        if not _tunnels_enabled:
            return _err("Tunnels are not enabled on this server (start with --tunnels)")

        from comfy_runner.tunnel import stop_tunnel

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        out, lines = _make_collector()

        log.info("Stopping tunnel for '%s'…", name)
        try:
            stop_tunnel(name, send_output=out)
            log.info("Tunnel stopped for '%s'", name)
            return jsonify({"ok": True, "output": lines})
        except Exception as e:
            log.error("Tunnel stop failed for '%s': %s", name, e)
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/snapshot  — list snapshots
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot", methods=["GET"])
    def route_snapshot_list(name: str) -> Any:
        from comfy_runner.snapshot import list_snapshots

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            entries = list_snapshots(record["path"])
            return jsonify({"ok": True, "snapshots": [
                {
                    "filename": e["filename"],
                    "createdAt": e["snapshot"]["createdAt"],
                    "trigger": e["snapshot"]["trigger"],
                    "label": e["snapshot"].get("label"),
                    "nodeCount": len(e["snapshot"].get("customNodes", [])),
                    "pipPackageCount": len(e["snapshot"].get("pipPackages", {})),
                }
                for e in entries
            ], "totalCount": len(entries)})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/snapshot/<id>  — show snapshot details
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/<snapshot_id>", methods=["GET"])
    def route_snapshot_show(name: str, snapshot_id: str) -> Any:
        from comfy_runner.snapshot import load_snapshot, resolve_snapshot_id

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            filename = resolve_snapshot_id(record["path"], snapshot_id)
            data = load_snapshot(record["path"], filename)
            return jsonify({"ok": True, "filename": filename, "snapshot": data})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/snapshot/<id>/diff  — diff against current
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/<snapshot_id>/diff", methods=["GET"])
    def route_snapshot_diff(name: str, snapshot_id: str) -> Any:
        from comfy_runner.snapshot import diff_against_current, load_snapshot, resolve_snapshot_id

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            filename = resolve_snapshot_id(record["path"], snapshot_id)
            target = load_snapshot(record["path"], filename)
            diff = diff_against_current(record["path"], target)
            return jsonify({"ok": True, "diff": diff})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/snapshot/<id>/diff/<other_id>  — diff two snapshots
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/<snapshot_id>/diff/<other_id>", methods=["GET"])
    def route_snapshot_diff_pair(name: str, snapshot_id: str, other_id: str) -> Any:
        from comfy_runner.snapshot import diff_snapshots, load_snapshot, resolve_snapshot_id

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            fn_a = resolve_snapshot_id(record["path"], snapshot_id)
            fn_b = resolve_snapshot_id(record["path"], other_id)
            snap_a = load_snapshot(record["path"], fn_a)
            snap_b = load_snapshot(record["path"], fn_b)
            diff = diff_snapshots(snap_a, snap_b)
            return jsonify({"ok": True, "diff": diff})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/snapshot/<id>/export  — export snapshot to JSON
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/<snapshot_id>/export", methods=["GET"])
    def route_snapshot_export(name: str, snapshot_id: str) -> Any:
        from comfy_runner.snapshot import build_export_envelope, load_snapshot, resolve_snapshot_id

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            filename = resolve_snapshot_id(record["path"], snapshot_id)
            snapshot = load_snapshot(record["path"], filename)
            envelope = build_export_envelope(name, [{"filename": filename, "snapshot": snapshot}])
            return jsonify({"ok": True, "envelope": envelope})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/snapshot/save
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/save", methods=["POST"])
    def route_snapshot_save(name: str) -> Any:
        from comfy_runner.config import get_installation, set_installation
        from comfy_runner.snapshot import get_snapshot_count, save_snapshot

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        label = body.get("label")

        try:
            filename = save_snapshot(record["path"], trigger="manual", label=label)
            rec = get_installation(name) or record
            rec["last_snapshot"] = filename
            rec["snapshot_count"] = get_snapshot_count(record["path"])
            set_installation(name, rec)
            return jsonify({"ok": True, "filename": filename})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/snapshot/restore  (async — returns job_id)
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/restore", methods=["POST"])
    def route_snapshot_restore(name: str) -> Any:
        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        snapshot_id = body.get("id", "")
        if not snapshot_id:
            return _err("Missing 'id' field")

        job_id = _jobs.create(label=f"snapshot restore {name}")

        def _run() -> None:
            from comfy_runner.config import get_installation, set_installation
            from comfy_runner.process import get_status, stop_installation
            from comfy_runner.snapshot import resolve_snapshot_id, restore_snapshot

            out, lines = _make_collector(job_id)
            lock = _get_install_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                return
            try:
                rec = get_installation(name)
                if not rec:
                    _jobs.fail(job_id, f"Installation '{name}' not found", lines)
                    return

                status = get_status(name)
                if status.get("running"):
                    stop_installation(name, send_output=out)

                filename = resolve_snapshot_id(rec["path"], snapshot_id)
                result = restore_snapshot(rec["path"], filename, send_output=out)
                # Track the restored snapshot and capture post-restore state
                rec = get_installation(name) or rec
                rec["last_snapshot"] = filename
                set_installation(name, rec)
                _capture_and_track(
                    name, rec["path"], "post-restore", out=out,
                )
                _jobs.finish(job_id, {"result": result}, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # POST /<name>/snapshot/import  (auto-restores unless restore=false)
    # ------------------------------------------------------------------
    @app.route("/<name>/snapshot/import", methods=["POST"])
    def route_snapshot_import(name: str) -> Any:
        from comfy_runner.snapshot import import_snapshots, validate_export_envelope

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        try:
            body = request.get_json(silent=True)
            if not body:
                return _err("Request body must be JSON")

            # Support envelope at top level or nested under "envelope"
            envelope_data = body.get("envelope", body)
            should_restore = body.get("restore", True)

            envelope = validate_export_envelope(envelope_data)
            import_result = import_snapshots(record["path"], envelope)

            if not should_restore or import_result.get("imported", 0) == 0:
                return jsonify({"ok": True, **import_result})

            # Auto-restore the first imported snapshot
            snapshot_filename = None
            for snap in envelope.get("snapshots", []):
                fn = snap.get("filename")
                if fn:
                    snapshot_filename = fn
                    break

            if not snapshot_filename:
                return jsonify({"ok": True, **import_result})

            # Kick off async restore
            job_id = _jobs.create(label=f"import + restore {name}")

            def _run() -> None:
                from comfy_runner.config import get_installation, set_installation
                from comfy_runner.process import get_status, stop_installation
                from comfy_runner.snapshot import resolve_snapshot_id, restore_snapshot

                out, lines = _make_collector(job_id)
                lock = _get_install_lock(name)
                if not lock.acquire(timeout=5):
                    _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                    return
                try:
                    rec = get_installation(name)
                    if not rec:
                        _jobs.fail(job_id, f"Installation '{name}' not found", lines)
                        return

                    status = get_status(name)
                    if status.get("running"):
                        stop_installation(name, send_output=out)

                    filename = resolve_snapshot_id(rec["path"], snapshot_filename)
                    result = restore_snapshot(rec["path"], filename, send_output=out)
                    rec = get_installation(name) or rec
                    rec["last_snapshot"] = filename
                    set_installation(name, rec)
                    _capture_and_track(
                        name, rec["path"], "post-restore", out=out,
                    )
                    _jobs.finish(job_id, {
                        "import": import_result,
                        "restore": result,
                    }, lines)
                except Exception as e:
                    _jobs.fail(job_id, str(e), lines)
                finally:
                    lock.release()

            threading.Thread(target=_run, daemon=True).start()
            return jsonify({
                "ok": True,
                "job_id": job_id,
                "async": True,
                **import_result,
            })

        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /<name>/download-model  (async — returns job_id)
    # ------------------------------------------------------------------
    @app.route("/<name>/download-model", methods=["POST"])
    def route_download_model(name: str) -> Any:
        from comfy_runner.workflow_models import (
            check_missing_models,
            download_models,
            resolve_models_dir,
        )
        from urllib.parse import urlparse, unquote

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        url = body.get("url", "")
        directory = body.get("directory", "")
        filename = body.get("name", "")

        if not url:
            return _err("Missing 'url' field")
        if not directory:
            return _err("Missing 'directory' field")
        if not filename:
            path = urlparse(url).path
            filename = unquote(path.rsplit("/", 1)[-1]) or "download"
            if "?" in filename:
                filename = filename.split("?")[0]

        try:
            models_dir = resolve_models_dir(record["path"])
        except Exception as e:
            return _err(str(e))

        token = body.get("token", "")

        model = {"name": filename, "url": url, "directory": directory}
        missing, existing = check_missing_models([model], models_dir)

        if not missing:
            return jsonify({"ok": True, "skipped": True, "name": filename, "directory": directory})

        job_id = _jobs.create(label=f"download-model {directory}/{filename}")
        cancel_event = _jobs.get_cancel_event(job_id)

        def _run() -> None:
            out, lines = _make_collector(job_id)
            try:
                dl_result = download_models(
                    missing, models_dir, send_output=out,
                    cancel_event=cancel_event,
                    token=token,
                )
                if dl_result.get("cancelled"):
                    _jobs.fail(job_id, "Cancelled by user", lines)
                else:
                    _jobs.finish(job_id, {
                        "name": filename,
                        "directory": directory,
                        **dl_result,
                    }, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "async": True,
            "name": filename,
            "directory": directory,
        })

    # ------------------------------------------------------------------
    # POST /<name>/upload-model
    # ------------------------------------------------------------------
    @app.route("/<name>/upload-model", methods=["POST"])
    def route_upload_model(name: str) -> Any:
        from comfy_runner.upload import receive_upload
        from comfy_runner.workflow_models import resolve_models_dir

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        if "file" not in request.files:
            return _err("Missing 'file' field in multipart form data")

        directory = request.form.get("directory", "")
        if not directory:
            return _err("Missing 'directory' field")

        filename = request.form.get("name", "")
        if not filename:
            uploaded = request.files["file"]
            filename = uploaded.filename or "upload"

        offset = int(request.form.get("offset", "0"))
        expected_hash = request.form.get("hash", "")
        hash_type = request.form.get("hash_type", "blake3")

        try:
            models_dir = resolve_models_dir(record["path"])
            file_stream = request.files["file"].stream
            result = receive_upload(
                models_dir, directory, filename, file_stream,
                offset=offset,
                expected_hash=expected_hash,
                hash_type=hash_type,
            )
            return jsonify({"ok": True, **result})
        except (ValueError, RuntimeError) as e:
            return _err(str(e))
        except Exception as e:
            log.error("Upload failed for '%s': %s", name, e)
            return _err(str(e))

    # ------------------------------------------------------------------
    # GET /<name>/upload-model/status
    # ------------------------------------------------------------------
    @app.route("/<name>/upload-model/status", methods=["GET"])
    def route_upload_status(name: str) -> Any:
        from comfy_runner.upload import get_upload_status
        from comfy_runner.workflow_models import resolve_models_dir

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        directory = request.args.get("directory", "")
        filename = request.args.get("name", "")
        if not directory or not filename:
            return _err("Missing 'directory' and 'name' query parameters")

        try:
            models_dir = resolve_models_dir(record["path"])
            status = get_upload_status(models_dir, directory, filename)
            return jsonify({"ok": True, **status})
        except (ValueError, RuntimeError) as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # DELETE /<name>/upload-model/status
    # ------------------------------------------------------------------
    @app.route("/<name>/upload-model/status", methods=["DELETE"])
    def route_upload_delete(name: str) -> Any:
        from comfy_runner.upload import delete_staging

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        directory = body.get("directory", "")
        filename = body.get("name", "")
        if not directory or not filename:
            return _err("Missing 'directory' and 'name' fields")

        removed = delete_staging(directory, filename)
        return jsonify({"ok": True, "removed": removed})

    # ------------------------------------------------------------------
    # POST /<name>/workflow-models  (async — returns job_id)
    # ------------------------------------------------------------------
    @app.route("/<name>/workflow-models", methods=["POST"])
    def route_workflow_models(name: str) -> Any:
        from comfy_runner.workflow_models import (
            check_missing_models,
            download_models,
            parse_workflow_models,
            resolve_models_dir,
        )

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        workflow = body.get("workflow")
        if not workflow or not isinstance(workflow, dict):
            return _err("Missing or invalid 'workflow' field")

        try:
            models = parse_workflow_models(workflow)
            models_dir = resolve_models_dir(record["path"])
            missing, _existing = check_missing_models(models, models_dir)
        except Exception as e:
            return _err(str(e))

        if not missing:
            return jsonify({
                "ok": True,
                "total": len(models),
                "missing": 0,
            })

        job_id = _jobs.create(label=f"workflow-models {name}")

        def _run() -> None:
            out, lines = _make_collector(job_id)
            cancel_event = _jobs.get_cancel_event(job_id)
            try:
                dl_result = download_models(
                    missing, models_dir, send_output=out,
                    cancel_event=cancel_event,
                )
                if dl_result.get("cancelled"):
                    _jobs.fail(job_id, "Cancelled by user", lines)
                else:
                    _jobs.finish(job_id, {
                        "total": len(models),
                        "missing": len(missing),
                        "models": models,
                        "missing_models": missing,
                    }, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({
            "ok": True,
            "job_id": job_id,
            "async": True,
            "total": len(models),
            "missing": len(missing),
        })

    # ------------------------------------------------------------------
    # POST /<name>/start  (async — returns job_id)
    # ------------------------------------------------------------------
    @app.route("/<name>/start", methods=["POST"])
    def route_start(name: str) -> Any:
        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        extra_args = body.get("extra_args")

        job_id = _jobs.create(label=f"start {name}")

        def _run() -> None:
            from comfy_runner.config import get_installation
            from comfy_runner.process import get_status, start_installation

            out, lines = _make_collector(job_id)
            lock = _get_install_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Installation '{name}' is busy", lines)
                return
            try:
                status = get_status(name)
                if status.get("running"):
                    _jobs.fail(job_id, f"Installation '{name}' is already running", lines)
                    return

                rec = get_installation(name)
                install_path = rec["path"] if rec else ""

                result = start_installation(name, extra_args=extra_args, send_output=out)
                if _tailscale_mode and result.get("port"):
                    try:
                        from comfy_runner.tunnel import start_tailscale_serve_port
                        ts_url = start_tailscale_serve_port(result["port"], send_output=out)
                        result["tailscale_url"] = ts_url
                    except Exception as e:
                        out(f"⚠ Tailscale serve failed: {e}\n")
                if install_path:
                    _capture_and_track(name, install_path, "start", out=out)
                _jobs.finish(job_id, result, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # GET/POST /<name>/comfyui/<path> — proxy to running ComfyUI instance
    # ------------------------------------------------------------------
    _HOP_BY_HOP_HEADERS = frozenset({
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade",
        "content-encoding", "content-length",
    })

    @app.route("/<name>/comfyui/", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "DELETE"])
    @app.route("/<name>/comfyui/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE"])
    def route_comfyui_proxy(name: str, subpath: str) -> Any:
        import requests as req_lib

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        from comfy_runner.process import get_status
        status = get_status(name)
        if not status.get("running"):
            return _err(f"Installation '{name}' is not running", 503)

        port = status["port"]
        target_url = f"http://127.0.0.1:{port}/{subpath}"
        if request.query_string:
            target_url += f"?{request.query_string.decode()}"

        try:
            resp = req_lib.request(
                method=request.method,
                url=target_url,
                headers={k: v for k, v in request.headers if k.lower() not in ("host", "content-length")},
                data=request.get_data(),
                timeout=120,
                stream=True,
            )
            from flask import Response
            fwd_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS
            }
            return Response(
                resp.iter_content(chunk_size=None),
                status=resp.status_code,
                headers=fwd_headers,
            )
        except req_lib.ConnectionError:
            return _err(f"Cannot connect to ComfyUI on port {port}", 502)
        except req_lib.Timeout:
            return _err("ComfyUI request timed out", 504)

    # ------------------------------------------------------------------
    # GET /<name>/outputs — list output files
    # GET /<name>/outputs/<path> — download a specific output file
    # ------------------------------------------------------------------
    def _get_output_dir(record: dict) -> Path:
        from comfy_runner.config import get_shared_dir
        shared_dir = get_shared_dir()
        if shared_dir:
            return Path(shared_dir) / "output"
        return Path(record["path"]) / "ComfyUI" / "output"

    @app.route("/<name>/outputs", methods=["GET"])
    def route_outputs_list(name: str) -> Any:
        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        output_dir = _get_output_dir(record)
        if not output_dir.exists():
            return jsonify({"ok": True, "output_dir": str(output_dir), "files": []})

        prefix = request.args.get("prefix", "")
        limit = request.args.get("limit", 50, type=int)
        after = request.args.get("after", type=float)

        # Collect file stats in one pass, then sort
        entries = []
        for p in output_dir.rglob("*"):
            if not p.is_file():
                continue
            st = p.stat()
            entries.append((p, st))
        entries.sort(key=lambda x: x[1].st_mtime, reverse=True)

        files = []
        for p, st in entries:
            if after is not None and st.st_mtime <= after:
                break
            rel = p.relative_to(output_dir)
            if prefix and not str(rel).startswith(prefix):
                continue
            files.append({
                "name": str(rel),
                "size": st.st_size,
                "modified": st.st_mtime,
            })
            if len(files) >= limit:
                break

        return jsonify({"ok": True, "output_dir": str(output_dir), "files": files})

    @app.route("/<name>/outputs/<path:filepath>", methods=["GET"])
    def route_outputs_download(name: str, filepath: str) -> Any:
        from flask import send_from_directory

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        output_dir = _get_output_dir(record)
        target = (output_dir / filepath).resolve()
        if not target.is_relative_to(output_dir.resolve()):
            return _err("Invalid path", 403)
        if not target.is_file():
            return _err(f"File not found: {filepath}", 404)

        return send_from_directory(str(output_dir), filepath)

    # ------------------------------------------------------------------
    # GET /config — view global config
    # PUT /config — update global config keys
    # ------------------------------------------------------------------
    @app.route("/config", methods=["GET"])
    def route_global_config_get() -> Any:
        from comfy_runner.config import get_shared_dir, get_hf_token, get_modelscope_token
        return jsonify({
            "ok": True,
            "config": {
                "shared_dir": get_shared_dir(),
                "hf_token": bool(get_hf_token()),
                "modelscope_token": bool(get_modelscope_token()),
            },
        })

    @app.route("/config", methods=["PUT"])
    def route_global_config_set() -> Any:
        from comfy_runner.config import (
            set_shared_dir, set_hf_token, set_modelscope_token,
        )
        body = request.get_json(silent=True) or {}
        allowed_keys = {"shared_dir", "hf_token", "modelscope_token"}
        updated = {}
        for key in allowed_keys:
            if key in body:
                value = body[key]
                if key == "shared_dir":
                    if value:
                        from comfy_runner.shared_paths import ensure_shared_dirs
                        resolved = str(Path(value).resolve())
                        ensure_shared_dirs(resolved)
                        set_shared_dir(resolved)
                        updated[key] = resolved
                    else:
                        set_shared_dir("")
                        updated[key] = ""
                elif key == "hf_token":
                    set_hf_token(value)
                    updated[key] = bool(value)
                elif key == "modelscope_token":
                    set_modelscope_token(value)
                    updated[key] = bool(value)
        if not updated:
            return _err(f"No valid keys. Allowed: {', '.join(sorted(allowed_keys))}")
        return jsonify({"ok": True, "updated": updated})

    # ------------------------------------------------------------------
    # Backwards-compat: un-prefixed routes default to first installation
    # ------------------------------------------------------------------
    def _default_name() -> str:
        from comfy_runner.installations import show_list
        installs = show_list()
        if installs:
            return installs[0]["name"]
        # Generate runner-N name matching local TUI convention
        n = 1
        existing = {inst["name"] for inst in installs}
        while f"runner-{n}" in existing:
            n += 1
        return f"runner-{n}"

    @app.route("/deploy", methods=["POST"])
    def route_deploy_default() -> Any:
        return route_deploy(_default_name())

    @app.route("/restart", methods=["POST"])
    def route_restart_default() -> Any:
        return route_restart(_default_name())

    @app.route("/stop", methods=["POST"])
    def route_stop_default() -> Any:
        return route_stop(_default_name())

    # ------------------------------------------------------------------
    # POST /self-update — git pull and restart the server process
    # ------------------------------------------------------------------
    @app.route("/self-update", methods=["POST"])
    def route_self_update() -> Any:
        import os
        import subprocess
        import sys

        repo_dir = Path(__file__).resolve().parent.parent

        # git pull
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(repo_dir),
                capture_output=True, text=True, timeout=30,
            )
            pull_output = result.stdout.strip()
            if result.returncode != 0:
                return _err(f"git pull failed: {result.stderr.strip()}")
        except Exception as e:
            return _err(f"git pull failed: {e}")

        already_up_to_date = "Already up to date" in pull_output

        if already_up_to_date:
            return jsonify({"ok": True, "updated": False, "message": pull_output})

        # Schedule restart after response is sent
        def _restart() -> None:
            time.sleep(1)
            log.info("Self-update: restarting server process...")
            if sys.platform == "win32":
                # os.execv on Windows doesn't preserve venv sys.path;
                # spawn a new process and exit instead.
                subprocess.Popen([sys.executable] + sys.argv)
                os._exit(0)
            else:
                os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=_restart, daemon=True).start()
        return jsonify({
            "ok": True,
            "updated": True,
            "message": pull_output,
            "restarting": True,
        })

    return app


# ---------------------------------------------------------------------------
# Standalone run helper
# ---------------------------------------------------------------------------

def run_server(
    host: str = "127.0.0.1",
    port: int = 9189,
) -> None:
    """Start the control server (blocking)."""
    from waitress import serve

    # Ensure job lifecycle messages appear on the console
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False  # Prevent duplicate output via root logger

    app = create_app()
    log.info("Starting comfy-runner control server on %s:%d", host, port)
    print(f"comfy-runner server listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8)
