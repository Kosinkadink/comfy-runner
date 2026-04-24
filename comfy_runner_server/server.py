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


def _validate_env_dict(env: Any) -> str | None:
    """Return an error message if *env* is not a valid ``dict[str, str]``, else None."""
    if env is None:
        return None
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in env.items()
    ):
        return "'env' must be a dict of string key-value pairs"
    return None


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


def _force_release_lock(name: str) -> bool:
    """Replace a stuck lock with a fresh one. Returns True if a lock existed."""
    with _install_locks_guard:
        if name in _install_locks:
            _install_locks[name] = threading.Lock()
            return True
        return False


# ---------------------------------------------------------------------------
# Test run index — tracks test runs separately from generic jobs
# ---------------------------------------------------------------------------

_test_runs_lock = threading.Lock()
_test_runs: dict[str, dict[str, Any]] = {}


def _register_test_run(job_id: str, meta: dict[str, Any]) -> None:
    """Register a test run keyed by its job_id."""
    with _test_runs_lock:
        _test_runs[job_id] = {
            "id": job_id,
            "created_at": time.time(),
            **meta,
        }


def _finish_test_run(job_id: str, updates: dict[str, Any], status: str = "done") -> None:
    """Update a test run with final results and persist status."""
    with _test_runs_lock:
        run = _test_runs.get(job_id)
        if run:
            run.update(updates)
            run["status"] = status
            run["finished_at"] = time.time()


_MAX_TEST_RUNS = 200


def _gc_test_runs() -> None:
    """Evict oldest test runs beyond the limit (caller must hold lock)."""
    if len(_test_runs) <= _MAX_TEST_RUNS:
        return
    by_time = sorted(_test_runs.keys(), key=lambda k: _test_runs[k].get("created_at", 0))
    to_remove = len(_test_runs) - _MAX_TEST_RUNS
    for k in by_time[:to_remove]:
        del _test_runs[k]


def _list_test_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Return test runs newest-first, merged with current job status."""
    with _test_runs_lock:
        _gc_test_runs()
        runs = sorted(
            _test_runs.values(),
            key=lambda r: r.get("created_at", 0),
            reverse=True,
        )[:limit]
    # Merge in current job status — prefer persisted status over live job
    enriched = []
    for run in runs:
        entry = dict(run)
        job = _jobs.get(run["id"])
        if job:
            entry["status"] = job["status"]
        elif "status" not in entry:
            entry["status"] = "expired"
        enriched.append(entry)
    return enriched


# Per-pod locks — prevent concurrent operations on the same pod
_pod_locks: dict[str, threading.Lock] = {}
_pod_locks_guard = threading.Lock()


def _get_pod_lock(name: str) -> threading.Lock:
    """Return a per-pod lock, creating one if needed."""
    with _pod_locks_guard:
        if name not in _pod_locks:
            _pod_locks[name] = threading.Lock()
        return _pod_locks[name]


def _remove_pod_lock(name: str) -> None:
    """Remove a pod lock entry after termination (only if unlocked)."""
    with _pod_locks_guard:
        lock = _pod_locks.get(name)
        if lock is not None and not lock.locked():
            _pod_locks.pop(name, None)


def _get_runpod_provider():
    """Create a RunPodProvider instance. Raises RuntimeError if not configured."""
    from comfy_runner.hosted.runpod_provider import RunPodProvider
    return RunPodProvider()


def _get_pod_server_url(pod_name: str) -> str:
    """Resolve the comfy-runner server URL for a named pod via Tailscale."""
    from comfy_runner.hosted.runpod_provider import RunPodProvider
    provider = RunPodProvider()
    url = provider.get_pod_tailscale_url(pod_name, port=9189)
    if url:
        return url
    # Fallback: try RunPod proxy via pod record
    from comfy_runner.hosted.config import get_pod_record
    rec = get_pod_record("runpod", pod_name)
    if rec and rec.get("id"):
        return f"https://{rec['id']}-9189.proxy.runpod.net"
    raise RuntimeError(f"Cannot resolve server URL for pod '{pod_name}'")


def _wait_for_remote_server(
    server_url: str,
    timeout: int = 300,
    poll_interval: int = 10,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Poll a remote comfy-runner server until it responds."""
    import requests as _requests
    out = send_output or (lambda _: None)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = _requests.get(f"{server_url}/system-info", timeout=5)
            if resp.ok:
                resp.json()
                out("Remote server is ready.\n")
                return
        except Exception:
            pass
        remaining = int(deadline - time.monotonic())
        out(f"\rWaiting for remote server... ({remaining}s remaining)")
        time.sleep(poll_interval)
    raise RuntimeError(f"Remote server at {server_url} did not become ready within {timeout}s")


def _build_test_target(target_body: dict[str, Any]):
    """Build a fleet TestTarget from a request body target dict."""
    from comfy_runner.testing.fleet import LocalTarget, RemoteTarget, EphemeralTarget
    kind = target_body.get("kind", "")

    if kind == "local":
        url = target_body.get("url", "")
        if not url:
            raise ValueError("local target requires 'url'")
        if "://" not in url:
            url = f"http://{url}"
        return LocalTarget(url=url, label=target_body.get("label"))

    elif kind == "remote":
        # Resolve by pod_name or explicit server_url
        pod_name = target_body.get("pod_name")
        server_url = target_body.get("server_url")
        if pod_name and not server_url:
            server_url = _get_pod_server_url(pod_name)
        if not server_url:
            raise ValueError("remote target requires 'pod_name' or 'server_url'")
        return RemoteTarget(
            server_url=server_url,
            install_name=target_body.get("install", "main"),
            label=target_body.get("label") or pod_name,
        )

    elif kind == "runpod":
        return EphemeralTarget(
            gpu_type=target_body.get("gpu_type", ""),
            label=target_body.get("label"),
            image=target_body.get("image"),
            volume_id=target_body.get("volume_id"),
            install_name=target_body.get("install", "main"),
        )

    else:
        raise ValueError(f"Unknown target kind: '{kind}'. Expected: local, remote, runpod")


def _validate_pod_name(name: str) -> str | None:
    """Validate a pod name from a URL path parameter.

    Returns an error message if invalid, else None.
    """
    from safe_file import is_safe_path_component
    if not name or not is_safe_path_component(name):
        return f"Invalid pod name: '{name}'"
    return None


_SUITES_DIR = Path("test-suites")


def _get_suites_dir() -> Path:
    """Return the managed test-suites directory, creating if needed."""
    d = _SUITES_DIR.resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_suite(suite: str) -> tuple[Path, str | None]:
    """Resolve a suite name or path to an absolute path.

    If ``suite`` is a bare name (no separators), look in the managed
    suites directory.  Otherwise treat it as a literal path.

    Returns ``(resolved_path, error_message)``.  error_message is None
    on success.
    """
    from safe_file import is_safe_path_component

    if not suite:
        return Path(), "'suite' is required"

    # Bare name → managed directory
    if is_safe_path_component(suite):
        candidate = _get_suites_dir() / suite
        if candidate.is_dir() and (candidate / "suite.json").is_file():
            return candidate.resolve(), None
        return Path(), f"Suite '{suite}' not found on server"

    # Literal path (backward compat for direct local use)
    suite_path = Path(suite).resolve()
    if not suite_path.is_dir():
        return Path(), f"Suite directory not found: {suite}"
    if not (suite_path / "suite.json").is_file():
        return Path(), f"Not a valid test suite (missing suite.json): {suite}"
    return suite_path, None


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>comfy-runner Dashboard</title>
<meta http-equiv="refresh" content="15">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
         background: #1a1a2e; color: #e0e0e0; margin: 2rem; }
  h1 { color: #00d9ff; }
  h2 { color: #7c8db5; border-bottom: 1px solid #2a2a4e; padding-bottom: 0.3rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 2rem; }
  th, td { text-align: left; padding: 0.5rem 1rem; border-bottom: 1px solid #2a2a4e; }
  th { color: #7c8db5; font-weight: 600; }
  .running { color: #4caf50; }
  .stopped, .exited { color: #ff9800; }
  .terminated, .error { color: #f44336; }
  .done { color: #4caf50; }
  .cancelled { color: #ff9800; }
  a { color: #00d9ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .refresh { color: #555; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>comfy-runner Dashboard</h1>
<p class="refresh">Auto-refreshes every 15 seconds</p>

<h2>Pods</h2>
{% if pods %}
<table>
<tr><th>Name</th><th>Status</th><th>GPU</th><th>$/hr</th><th>Server URL</th></tr>
{% for p in pods %}
<tr>
  <td>{{ p.name }}</td>
  <td class="{{ p.status|lower }}">{{ p.status }}</td>
  <td>{{ p.gpu_type }}</td>
  <td>{{ "%.2f"|format(p.cost_per_hr) }}</td>
  <td>{% if p.server_url %}<a href="{{ p.server_url }}">{{ p.server_url }}</a>{% else %}-{% endif %}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No pods configured.</p>
{% endif %}

<h2>Recent Test Runs</h2>
{% if test_runs %}
<table>
<tr><th>ID</th><th>Kind</th><th>Suite</th><th>Status</th><th>Targets</th></tr>
{% for t in test_runs %}
<tr>
  <td><a href="/tests/{{ t.id }}">{{ t.id }}</a></td>
  <td>{{ t.kind }}</td>
  <td>{{ t.suite }}</td>
  <td class="{{ t.status }}">{{ t.status }}</td>
  <td>{{ t.targets|length }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No test runs.</p>
{% endif %}

<h2>Active Jobs</h2>
{% if jobs %}
<table>
<tr><th>ID</th><th>Label</th><th>Status</th><th>Started</th></tr>
{% for j in jobs %}
<tr>
  <td><a href="/job/{{ j.id }}">{{ j.id }}</a></td>
  <td>{{ j.label }}</td>
  <td class="{{ j.status }}">{{ j.status }}</td>
  <td>{{ j.started_at|int }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No active jobs.</p>
{% endif %}

</body>
</html>
"""


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

    @app.route("/startup-log", methods=["GET"])
    def route_startup_log() -> Any:
        log_path = Path("/tmp/comfy-runner-startup.log")
        if not log_path.is_file():
            return _err("No startup log found", 404)
        lines = int(request.args.get("lines", 200))
        content = log_path.read_text(errors="replace")
        all_lines = content.splitlines()
        if lines > 0:
            all_lines = all_lines[-lines:]
        return jsonify({"ok": True, "lines": all_lines})

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
                "env": record.get("env", {}),
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
                        variant = body.get("variant")
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
                            variant=variant,
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
        env_overrides = body.get("env")
        env_err = _validate_env_dict(env_overrides)
        if env_err:
            return _err(env_err)

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

                result = start_installation(name, port_override=running_port, extra_args=extra_args, env_overrides=env_overrides, send_output=out)
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
    # POST /<name>/unlock — force-release a stuck installation lock
    # ------------------------------------------------------------------
    @app.route("/<name>/unlock", methods=["POST"])
    def route_unlock(name: str) -> Any:
        had_lock = _force_release_lock(name)
        return jsonify({"ok": True, "lock_reset": had_lock})

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
        allowed_keys = {"launch_args", "env"}
        updated = {}
        for key in allowed_keys:
            if key in body:
                if key == "env":
                    env_err = _validate_env_dict(body[key])
                    if env_err:
                        return _err(env_err)
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
    # POST /<name>/move-model
    # ------------------------------------------------------------------
    @app.route("/<name>/move-model", methods=["POST"])
    def route_move_model(name: str) -> Any:
        from comfy_runner.workflow_models import resolve_models_dir, _validate_model_path

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        body = request.get_json(silent=True) or {}
        from_directory = body.get("from_directory", "")
        to_directory = body.get("to_directory", "")
        filename = body.get("name", "")
        copy = body.get("copy", False)

        if not from_directory:
            return _err("Missing 'from_directory' field")
        if not to_directory:
            return _err("Missing 'to_directory' field")
        if not filename:
            return _err("Missing 'name' field")

        try:
            models_dir = resolve_models_dir(record["path"])

            src = _validate_model_path(models_dir, from_directory, filename)
            dst = _validate_model_path(models_dir, to_directory, filename)

            if not src.is_file():
                return _err(f"Source not found: {from_directory}/{filename}", 404)

            dst.parent.mkdir(parents=True, exist_ok=True)

            if dst.exists():
                return _err(f"Destination already exists: {to_directory}/{filename}")

            import shutil
            if copy:
                shutil.copy2(str(src), str(dst))
            else:
                shutil.move(str(src), str(dst))

            action = "copied" if copy else "moved"
            return jsonify({
                "ok": True,
                "action": action,
                "name": filename,
                "from_directory": from_directory,
                "to_directory": to_directory,
            })
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            log.error("Move/copy failed for '%s': %s", name, e)
            return _err(str(e))

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
        env_overrides = body.get("env")
        env_err = _validate_env_dict(env_overrides)
        if env_err:
            return _err(env_err)

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

                result = start_installation(name, extra_args=extra_args, env_overrides=env_overrides, send_output=out)
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
    # POST /test/run — run test suite (async)
    # ------------------------------------------------------------------
    @app.route("/test/run", methods=["POST"])
    def route_test_run() -> Any:
        from comfy_runner.installations import show_list

        body = request.get_json(silent=True) or {}
        suite_path = body.get("suite")
        if not suite_path:
            return _err("'suite' is required", 400)

        name = body.get("name")
        if not name:
            installs = show_list()
            if not installs:
                return _err("No installations available", 400)
            name = installs[0]["name"]

        timeout = body.get("timeout", 600)
        http_timeout = body.get("http_timeout", 30)
        formats = body.get("formats", "json,html,markdown")

        if not isinstance(timeout, int) or timeout <= 0:
            return _err("'timeout' must be a positive integer", 400)
        if not isinstance(http_timeout, int) or http_timeout <= 0:
            return _err("'http_timeout' must be a positive integer", 400)
        if not isinstance(formats, str) or not formats.strip():
            return _err("'formats' must be a non-empty string", 400)

        record, err = _get_record(name)
        if not record:
            return _err(err, 404)

        from comfy_runner.process import get_status
        status = get_status(name)
        if not status.get("running"):
            return _err(f"Installation '{name}' is not running", 503)

        suite_basename = Path(suite_path).name
        job_id = _jobs.create(label=f"test run {suite_basename} on {name}")

        def _run() -> None:
            out, lines = _make_collector(job_id)
            try:
                from comfy_runner.testing.client import ComfyTestClient
                from comfy_runner.testing.runner import run_suite
                from comfy_runner.testing.suite import load_suite
                from comfy_runner.testing.report import build_report, write_report

                try:
                    suite = load_suite(suite_path)
                except ValueError as e:
                    _jobs.fail(job_id, str(e), lines)
                    return

                # Re-check status inside the thread to get current port
                cur_status = get_status(name)
                if not cur_status.get("running"):
                    _jobs.fail(job_id, f"Installation '{name}' stopped before test could start", lines)
                    return
                port = cur_status["port"]

                comfy_url = f"http://127.0.0.1:{port}"
                client = ComfyTestClient(comfy_url, timeout=http_timeout)

                from datetime import datetime, timezone
                run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                out_dir = Path(suite_path) / "runs" / run_id

                suite_run = run_suite(client, suite, out_dir, timeout=timeout, send_output=out)
                report = build_report(suite_run, target_info={"name": name, "url": comfy_url})

                formats_list = [f.strip() for f in formats.split(",")]
                written = write_report(report, out_dir, formats=formats_list)

                result = {
                    "run_id": run_id,
                    "suite_name": suite.name,
                    "output_dir": str(out_dir),
                    "total": report.total,
                    "passed": report.passed,
                    "failed": report.failed,
                    "duration": report.duration,
                    "report_files": {fmt: str(p) for fmt, p in written.items()},
                    "report": report.to_dict(),
                }
                _jobs.finish(job_id, result, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # GET /test/results/<run_id> — retrieve test run results
    # ------------------------------------------------------------------
    @app.route("/test/results/<run_id>", methods=["GET"])
    def route_test_results(run_id: str) -> Any:
        from safe_file import is_safe_path_component

        suite = request.args.get("suite")
        if not suite:
            return _err("'suite' query parameter is required", 400)

        if not is_safe_path_component(run_id):
            return _err("Invalid run_id", 400)

        suite_path = Path(suite).resolve()
        run_dir = suite_path / "runs" / run_id
        if not run_dir.resolve().is_relative_to(suite_path):
            return _err("Invalid run_id", 400)
        if not run_dir.is_dir():
            return _err(f"Run '{run_id}' not found", 404)

        fmt = request.args.get("format")
        if fmt == "json":
            import json as _json
            report_file = run_dir / "report.json"
            if not report_file.is_file():
                return _err("report.json not found in run directory", 404)
            data = _json.loads(report_file.read_text(encoding="utf-8"))
            return jsonify({"ok": True, "run_id": run_id, "report": data})

        files = [
            {"name": f.name, "size": f.stat().st_size}
            for f in sorted(run_dir.iterdir()) if f.is_file()
        ]
        return jsonify({"ok": True, "run_id": run_id, "output_dir": str(run_dir), "files": files})

    # ------------------------------------------------------------------
    # GET /test/suites — list available test suites
    # ------------------------------------------------------------------
    @app.route("/test/suites", methods=["GET"])
    def route_test_suites() -> Any:
        from comfy_runner.testing.suite import discover_suites

        search_dir = Path(request.args.get("dir", "."))
        suites = discover_suites(search_dir)
        return jsonify({"ok": True, "suites": [
            {
                "name": s.name,
                "path": str(s.path),
                "description": s.description,
                "workflows": len(s.workflows),
                "required_models": s.required_models,
            }
            for s in suites
        ]})

    # ==================================================================
    # Central Orchestration — Pod Management
    # ==================================================================

    # ------------------------------------------------------------------
    # GET /pods — list all pods with status
    # ------------------------------------------------------------------
    @app.route("/pods", methods=["GET"])
    def route_pods_list() -> Any:
        from comfy_runner.hosted.config import list_pod_records

        try:
            provider = _get_runpod_provider()
        except RuntimeError as e:
            return _err(str(e))

        try:
            live_pods = provider.list_pods()
            live_map = {p.name: p for p in live_pods}

            records = list_pod_records("runpod")
            result = []

            # Merge config records with live pod data
            seen_names = set()
            for name, rec in records.items():
                seen_names.add(name)
                pod_id = rec.get("id", "")
                live = live_map.get(name)
                # Also try matching by ID
                if not live:
                    for lp in live_pods:
                        if lp.id == pod_id:
                            live = lp
                            break
                entry: dict[str, Any] = {
                    "name": name,
                    "id": pod_id,
                    "gpu_type": rec.get("gpu_type", ""),
                    "datacenter": rec.get("datacenter", ""),
                    "image": rec.get("image", ""),
                }
                if live:
                    entry["status"] = live.status
                    entry["cost_per_hr"] = live.cost_per_hr
                    entry["gpu_type"] = live.gpu_type or entry["gpu_type"]
                else:
                    entry["status"] = "UNKNOWN"
                # Add URLs
                ts_url = provider.get_pod_tailscale_url(name, port=9189)
                if ts_url:
                    entry["server_url"] = ts_url
                    entry["comfy_url"] = ts_url.rsplit(":", 1)[0] + ":8188"
                result.append(entry)

            return jsonify({"ok": True, "pods": result})
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /pods/create — provision a RunPod pod (async)
    # ------------------------------------------------------------------
    @app.route("/pods/create", methods=["POST"])
    def route_pods_create() -> Any:
        from safe_file import is_safe_path_component

        body = request.get_json(silent=True) or {}
        name = body.get("name", "")
        if not name or not is_safe_path_component(name):
            return _err("'name' is required and must be a safe identifier")

        gpu_type = body.get("gpu_type")
        image = body.get("image")
        volume_id = body.get("volume_id")
        volume_size_gb = body.get("volume_size_gb")
        datacenter = body.get("datacenter")
        cloud_type = body.get("cloud_type")
        gpu_count = body.get("gpu_count", 1)
        env = body.get("env")
        wait_ready = body.get("wait_ready", True)

        job_id = _jobs.create(label=f"pod create {name}")

        def _run() -> None:
            from comfy_runner.hosted.config import get_pod_record, set_pod_record

            out, lines = _make_collector(job_id)
            lock = _get_pod_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Pod '{name}' is busy", lines)
                return
            try:
                provider = _get_runpod_provider()

                # Check if pod already exists
                rec = get_pod_record("runpod", name)
                if rec:
                    pod = provider.get_pod(rec["id"])
                    if pod and pod.status not in ("TERMINATED", "EXITED"):
                        if pod.status != "RUNNING":
                            out(f"Pod '{name}' exists but is {pod.status}, starting...\n")
                            provider.start_pod(rec["id"])
                        else:
                            out(f"Pod '{name}' already running.\n")
                        server_url = provider.get_pod_tailscale_url(name, port=9189) or ""
                        if wait_ready and server_url:
                            _wait_for_remote_server(server_url, send_output=out)
                        _jobs.finish(job_id, {
                            "name": name,
                            "id": rec["id"],
                            "status": "RUNNING",
                            "server_url": server_url,
                            "reused": True,
                        }, lines)
                        return

                out(f"Creating pod '{name}'...\n")
                pod = provider.create_pod(
                    name=name,
                    gpu_type=gpu_type,
                    image=image,
                    volume_id=volume_id,
                    volume_size_gb=volume_size_gb,
                    datacenter=datacenter,
                    cloud_type=cloud_type,
                    gpu_count=gpu_count,
                    env=env,
                )
                set_pod_record("runpod", name, {
                    "id": pod.id,
                    "gpu_type": pod.gpu_type,
                    "datacenter": pod.datacenter,
                    "image": pod.image,
                })
                out(f"Pod created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)\n")

                server_url = provider.get_pod_tailscale_url(name, port=9189) or ""
                if wait_ready and server_url:
                    _wait_for_remote_server(server_url, send_output=out)

                _jobs.finish(job_id, {
                    "name": name,
                    "id": pod.id,
                    "status": "RUNNING",
                    "gpu_type": pod.gpu_type,
                    "cost_per_hr": pod.cost_per_hr,
                    "server_url": server_url,
                    "reused": False,
                }, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True, "name": name})

    # ------------------------------------------------------------------
    # POST /pods/<name>/deploy — deploy a PR/branch/commit to a pod (async)
    # ------------------------------------------------------------------
    @app.route("/pods/<name>/deploy", methods=["POST"])
    def route_pods_deploy(name: str) -> Any:
        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        from comfy_runner.hosted.config import get_pod_record

        rec = get_pod_record("runpod", name)
        if not rec:
            return _err(f"Pod '{name}' not found in config", 404)

        body = request.get_json(silent=True) or {}
        install_name = body.get("install", "main")

        # Validate deploy mode
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

        job_id = _jobs.create(label=f"pod deploy {name}")

        def _run() -> None:
            from comfy_runner.hosted.remote import RemoteRunner

            out, lines = _make_collector(job_id)
            lock = _get_pod_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Pod '{name}' is busy", lines)
                return
            try:
                provider = _get_runpod_provider()
                pod_id = rec["id"]

                # Ensure pod is running
                pod = provider.get_pod(pod_id)
                if not pod or pod.status in ("TERMINATED", "EXITED"):
                    _jobs.fail(job_id, f"Pod '{name}' is terminated or gone", lines)
                    return
                if pod.status != "RUNNING":
                    out(f"Pod is {pod.status}, starting...\n")
                    provider.start_pod(pod_id)

                server_url = _get_pod_server_url(name)
                out(f"Connecting to {server_url}...\n")
                _wait_for_remote_server(server_url, send_output=out)

                runner = RemoteRunner(server_url)

                # Forward deploy
                deploy_body: dict[str, Any] = {}
                if pr is not None:
                    deploy_body["pr"] = pr
                if branch:
                    deploy_body["branch"] = branch
                if tag:
                    deploy_body["tag"] = tag
                if commit:
                    deploy_body["commit"] = commit
                if reset:
                    deploy_body["reset"] = True
                if latest:
                    deploy_body["latest"] = True
                if pull:
                    deploy_body["pull"] = True
                if body.get("start", True):
                    deploy_body["start"] = True
                if body.get("repo"):
                    deploy_body["repo"] = body["repo"]
                if body.get("title"):
                    deploy_body["title"] = body["title"]
                if body.get("launch_args"):
                    deploy_body["launch_args"] = body["launch_args"]
                if body.get("cuda_compat"):
                    deploy_body["cuda_compat"] = True
                if body.get("build"):
                    deploy_body["build"] = True
                    for bk in ("python_version", "pbs_release", "gpu",
                                "cuda_tag", "torch_version", "torch_spec",
                                "torch_index_url", "comfyui_ref"):
                        if bk in body:
                            deploy_body[bk] = body[bk]

                out(f"Deploying to '{install_name}' on pod '{name}'...\n")
                data = runner._request("POST", f"/{install_name}/deploy", json=deploy_body)

                remote_job_id = data.get("job_id")
                if remote_job_id:
                    out(f"Remote job started: {remote_job_id}\n")
                    result = runner.poll_job(
                        remote_job_id, timeout=600, on_output=out,
                    )
                    _jobs.finish(job_id, {
                        "pod_name": name,
                        "server_url": server_url,
                        "deploy_result": result,
                    }, lines)
                else:
                    _jobs.finish(job_id, {
                        "pod_name": name,
                        "server_url": server_url,
                        "deploy_result": data,
                    }, lines)

            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True, "name": name})

    # ------------------------------------------------------------------
    # POST /pods/<name>/stop — stop a pod (keep data)
    # ------------------------------------------------------------------
    @app.route("/pods/<name>/stop", methods=["POST"])
    def route_pods_stop(name: str) -> Any:
        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        from comfy_runner.hosted.config import get_pod_record

        rec = get_pod_record("runpod", name)
        if not rec:
            return _err(f"Pod '{name}' not found in config", 404)

        lock = _get_pod_lock(name)
        if not lock.acquire(timeout=5):
            return _err(f"Pod '{name}' is busy")
        try:
            provider = _get_runpod_provider()
            provider.stop_pod(rec["id"])
            return jsonify({"ok": True, "name": name, "action": "stopped"})
        except Exception as e:
            return _err(str(e))
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # POST /pods/<name>/start — start a stopped pod
    # ------------------------------------------------------------------
    @app.route("/pods/<name>/start", methods=["POST"])
    def route_pods_start(name: str) -> Any:
        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        from comfy_runner.hosted.config import get_pod_record

        rec = get_pod_record("runpod", name)
        if not rec:
            return _err(f"Pod '{name}' not found in config", 404)

        body = request.get_json(silent=True) or {}
        wait_ready = body.get("wait_ready", True)

        job_id = _jobs.create(label=f"pod start {name}")

        def _run() -> None:
            out, lines = _make_collector(job_id)
            lock = _get_pod_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Pod '{name}' is busy", lines)
                return
            try:
                provider = _get_runpod_provider()
                out(f"Starting pod '{name}'...\n")
                provider.start_pod(rec["id"])

                server_url = provider.get_pod_tailscale_url(name, port=9189) or ""
                if wait_ready and server_url:
                    _wait_for_remote_server(server_url, send_output=out)

                _jobs.finish(job_id, {
                    "name": name,
                    "id": rec["id"],
                    "status": "RUNNING",
                    "server_url": server_url,
                }, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True, "name": name})

    # ------------------------------------------------------------------
    # DELETE /pods/<name> — terminate a pod
    # ------------------------------------------------------------------
    @app.route("/pods/<name>", methods=["DELETE"])
    def route_pods_terminate(name: str) -> Any:
        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        from comfy_runner.hosted.config import get_pod_record, remove_pod_record

        rec = get_pod_record("runpod", name)
        if not rec:
            return _err(f"Pod '{name}' not found in config", 404)

        lock = _get_pod_lock(name)
        if not lock.acquire(timeout=5):
            return _err(f"Pod '{name}' is busy")
        try:
            provider = _get_runpod_provider()
            provider.terminate_pod(rec["id"])
            remove_pod_record("runpod", name)
            return jsonify({"ok": True, "name": name, "action": "terminated"})
        except Exception as e:
            return _err(str(e))
        finally:
            lock.release()
            _remove_pod_lock(name)

    # ------------------------------------------------------------------
    # POST /pods/cleanup — terminate orphaned test pods
    # ------------------------------------------------------------------
    @app.route("/pods/cleanup", methods=["POST"])
    def route_pods_cleanup() -> Any:
        body = request.get_json(silent=True) or {}
        prefix = body.get("prefix", "test-")
        dry_run = body.get("dry_run", False)
        max_age_hours = body.get("max_age_hours")

        try:
            provider = _get_runpod_provider()
            live_pods = provider.list_pods()

            candidates = []
            for pod in live_pods:
                if not pod.name.startswith(prefix):
                    continue
                if pod.status in ("TERMINATED",):
                    continue
                candidates.append(pod)

            terminated = []
            skipped = []
            for pod in candidates:
                if dry_run:
                    skipped.append({"name": pod.name, "id": pod.id, "status": pod.status})
                else:
                    try:
                        provider.terminate_pod(pod.id)
                        terminated.append({"name": pod.name, "id": pod.id})
                    except Exception as e:
                        skipped.append({"name": pod.name, "id": pod.id, "error": str(e)})

            return jsonify({
                "ok": True,
                "prefix": prefix,
                "dry_run": dry_run,
                "terminated": terminated,
                "skipped": skipped,
                "total_found": len(candidates),
                "total_terminated": len(terminated),
            })
        except Exception as e:
            return _err(str(e))

    # ==================================================================
    # Central Orchestration — Suite Management
    # ==================================================================

    # ------------------------------------------------------------------
    # GET /suites — list available test suites
    # ------------------------------------------------------------------
    @app.route("/suites", methods=["GET"])
    def route_suites_list() -> Any:
        suites_dir = _get_suites_dir()
        result = []
        for d in sorted(suites_dir.iterdir()):
            suite_json = d / "suite.json"
            if d.is_dir() and suite_json.is_file():
                import json as _json
                try:
                    meta = _json.loads(suite_json.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
                workflows_dir = d / "workflows"
                workflow_count = len(list(workflows_dir.glob("*.json"))) if workflows_dir.is_dir() else 0
                runs_dir = d / "runs"
                run_count = len(list(runs_dir.iterdir())) if runs_dir.is_dir() else 0
                result.append({
                    "name": d.name,
                    "title": meta.get("name", d.name),
                    "description": meta.get("description", ""),
                    "required_models": meta.get("required_models", []),
                    "workflow_count": workflow_count,
                    "run_count": run_count,
                })
        return jsonify({"ok": True, "suites": result})

    # ------------------------------------------------------------------
    # GET /suites/<name> — get suite details
    # ------------------------------------------------------------------
    @app.route("/suites/<name>", methods=["GET"])
    def route_suites_get(name: str) -> Any:
        from safe_file import is_safe_path_component
        if not is_safe_path_component(name):
            return _err(f"Invalid suite name: '{name}'")

        suite_dir = _get_suites_dir() / name
        suite_json = suite_dir / "suite.json"
        if not suite_dir.is_dir() or not suite_json.is_file():
            return _err(f"Suite '{name}' not found", 404)

        import json as _json
        meta = _json.loads(suite_json.read_text(encoding="utf-8"))

        config = {}
        config_path = suite_dir / "config.json"
        if config_path.is_file():
            config = _json.loads(config_path.read_text(encoding="utf-8"))

        workflows = []
        workflows_dir = suite_dir / "workflows"
        if workflows_dir.is_dir():
            for wf in sorted(workflows_dir.glob("*.json")):
                workflows.append(wf.name)

        runs = []
        runs_dir = suite_dir / "runs"
        if runs_dir.is_dir():
            for rd in sorted(runs_dir.iterdir(), reverse=True):
                if rd.is_dir():
                    runs.append(rd.name)

        return jsonify({
            "ok": True,
            "name": name,
            "suite": meta,
            "config": config,
            "workflows": workflows,
            "runs": runs,
        })

    # ------------------------------------------------------------------
    # POST /suites/<name> — upload/update a suite (preserves runs/)
    # ------------------------------------------------------------------
    @app.route("/suites/<name>", methods=["POST"])
    def route_suites_upload(name: str) -> Any:
        from safe_file import is_safe_path_component
        if not is_safe_path_component(name):
            return _err(f"Invalid suite name: '{name}'")

        body = request.get_json(silent=True) or {}
        suite_meta = body.get("suite")
        if not suite_meta or not isinstance(suite_meta, dict):
            return _err("'suite' is required (object with suite.json contents)")

        workflows = body.get("workflows")
        if not workflows or not isinstance(workflows, dict):
            return _err("'workflows' is required (object mapping filename → workflow JSON)")

        suite_dir = _get_suites_dir() / name
        suite_dir.mkdir(parents=True, exist_ok=True)

        import json as _json

        # Write suite.json
        (suite_dir / "suite.json").write_text(
            _json.dumps(suite_meta, indent=2), encoding="utf-8"
        )

        # Write config.json (optional)
        config = body.get("config")
        if config and isinstance(config, dict):
            (suite_dir / "config.json").write_text(
                _json.dumps(config, indent=2), encoding="utf-8"
            )

        # Write workflows — replace all workflow files
        wf_dir = suite_dir / "workflows"
        if wf_dir.is_dir():
            for old_wf in wf_dir.glob("*.json"):
                old_wf.unlink()
        wf_dir.mkdir(exist_ok=True)

        for wf_name, wf_data in workflows.items():
            if not is_safe_path_component(wf_name):
                return _err(f"Invalid workflow filename: '{wf_name}'")
            (wf_dir / wf_name).write_text(
                _json.dumps(wf_data, indent=2), encoding="utf-8"
            )

        return jsonify({
            "ok": True,
            "name": name,
            "workflows": list(workflows.keys()),
            "message": f"Suite '{name}' uploaded ({len(workflows)} workflow(s))",
        })

    # ------------------------------------------------------------------
    # DELETE /suites/<name> — remove a suite (preserves runs/)
    # ------------------------------------------------------------------
    @app.route("/suites/<name>", methods=["DELETE"])
    def route_suites_delete(name: str) -> Any:
        from safe_file import is_safe_path_component
        if not is_safe_path_component(name):
            return _err(f"Invalid suite name: '{name}'")

        suite_dir = _get_suites_dir() / name
        if not suite_dir.is_dir():
            return _err(f"Suite '{name}' not found", 404)

        force = request.args.get("force", "").lower() in ("true", "1", "yes")
        runs_dir = suite_dir / "runs"
        has_runs = runs_dir.is_dir() and any(runs_dir.iterdir())

        if has_runs and not force:
            run_count = len(list(runs_dir.iterdir()))
            return _err(
                f"Suite '{name}' has {run_count} test run(s). "
                f"Add ?force=true to delete the suite definition and keep runs, "
                f"or ?force=true&include_runs=true to delete everything."
            )

        include_runs = request.args.get("include_runs", "").lower() in ("true", "1", "yes")

        # Remove definition files only (preserve runs/)
        for f in ("suite.json", "config.json"):
            p = suite_dir / f
            if p.is_file():
                p.unlink()

        wf_dir = suite_dir / "workflows"
        if wf_dir.is_dir():
            import shutil
            shutil.rmtree(wf_dir)

        if include_runs and runs_dir.is_dir():
            import shutil
            shutil.rmtree(runs_dir)

        # Remove the directory if empty
        remaining = list(suite_dir.iterdir())
        if not remaining:
            suite_dir.rmdir()
            return jsonify({"ok": True, "name": name, "action": "deleted"})

        return jsonify({
            "ok": True,
            "name": name,
            "action": "definition_removed",
            "message": f"Suite definition removed. Runs preserved in {name}/runs/",
        })

    # ==================================================================
    # Central Orchestration — Test Execution
    # ==================================================================

    # ------------------------------------------------------------------
    # POST /tests/run — run a test suite against a single target (async)
    # ------------------------------------------------------------------
    @app.route("/tests/run", methods=["POST"])
    def route_tests_run() -> Any:
        body = request.get_json(silent=True) or {}
        suite_name = body.get("suite", "")
        suite_path, suite_err = _resolve_suite(suite_name)
        if suite_err:
            return _err(suite_err)

        target_body = body.get("target")
        if not target_body or not isinstance(target_body, dict):
            return _err("'target' is required (object with 'kind' field)")

        try:
            target = _build_test_target(target_body)
        except (ValueError, RuntimeError) as e:
            return _err(str(e))

        timeout = body.get("timeout", 600)
        formats = body.get("formats", "json,html,markdown")

        job_id = _jobs.create(label=f"test run {target.name}")
        _register_test_run(job_id, {
            "kind": "single",
            "suite": suite_name,
            "targets": [target_body],
        })

        def _run() -> None:
            from comfy_runner.testing.suite import load_suite
            out, lines = _make_collector(job_id)
            try:
                s = load_suite(str(suite_path))
                run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                out_dir = suite_path / "runs" / run_id

                result = target.run(
                    suite=s,
                    output_dir=out_dir,
                    timeout=timeout,
                    send_output=out,
                )

                summary = result.to_dict()
                _finish_test_run(job_id, {
                    "output_dir": str(out_dir),
                    "summary": summary,
                })
                _jobs.finish(job_id, {
                    "run_id": run_id,
                    "suite_name": s.name,
                    "output_dir": str(out_dir),
                    **summary,
                }, lines)
            except Exception as e:
                _finish_test_run(job_id, {"error": str(e)}, status="error")
                _jobs.fail(job_id, str(e), lines)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # POST /tests/fleet — run a suite across multiple targets (async)
    # ------------------------------------------------------------------
    @app.route("/tests/fleet", methods=["POST"])
    def route_tests_fleet() -> Any:
        from comfy_runner.testing.fleet import run_fleet

        body = request.get_json(silent=True) or {}
        suite_name = body.get("suite", "")
        suite_path, suite_err = _resolve_suite(suite_name)
        if suite_err:
            return _err(suite_err)

        target_bodies = body.get("targets", [])
        if not target_bodies or not isinstance(target_bodies, list):
            return _err("'targets' is required (list of target objects)")

        try:
            targets = [_build_test_target(t) for t in target_bodies]
        except (ValueError, RuntimeError) as e:
            return _err(str(e))

        timeout = body.get("timeout", 600)
        max_workers = body.get("max_workers")
        formats = body.get("formats", "json,html,markdown")

        target_names = [t.name for t in targets]
        job_id = _jobs.create(label=f"fleet test ({len(targets)} targets)")
        _register_test_run(job_id, {
            "kind": "fleet",
            "suite": suite_name,
            "targets": target_bodies,
        })

        def _run() -> None:
            out, lines = _make_collector(job_id)
            try:
                run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                out_dir = suite_path / "runs" / f"fleet-{run_id}"

                fleet_result = run_fleet(
                    targets=targets,
                    suite_path=str(suite_path),
                    output_dir=out_dir,
                    timeout=timeout,
                    max_workers=max_workers,
                    send_output=out,
                    formats=formats,
                )

                summary = fleet_result.to_dict()
                _finish_test_run(job_id, {
                    "output_dir": str(out_dir),
                    "summary": summary,
                })
                _jobs.finish(job_id, {
                    "run_id": run_id,
                    "output_dir": str(out_dir),
                    **summary,
                }, lines)
            except Exception as e:
                _finish_test_run(job_id, {"error": str(e)}, status="error")
                _jobs.fail(job_id, str(e), lines)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True, "targets": target_names})

    # ------------------------------------------------------------------
    # GET /tests — list recent/active test runs
    # ------------------------------------------------------------------
    @app.route("/tests", methods=["GET"])
    def route_tests_list() -> Any:
        limit = request.args.get("limit", 50, type=int)
        runs = _list_test_runs(limit=limit)
        return jsonify({"ok": True, "runs": runs})

    # ------------------------------------------------------------------
    # GET /tests/<test_id> — poll test status + metadata
    # ------------------------------------------------------------------
    @app.route("/tests/<test_id>", methods=["GET"])
    def route_tests_get(test_id: str) -> Any:
        with _test_runs_lock:
            run = _test_runs.get(test_id)
        if not run:
            return _err(f"Test run '{test_id}' not found", 404)

        entry = dict(run)
        job = _jobs.get(test_id)
        if job:
            entry["status"] = job["status"]
            entry["output"] = job.get("output", [])
            if job.get("result"):
                entry["result"] = job["result"]
            if job.get("error"):
                entry["error"] = job["error"]
        elif "status" not in entry:
            entry["status"] = "expired"

        return jsonify({"ok": True, **entry})

    # ------------------------------------------------------------------
    # GET /tests/<test_id>/report — retrieve test report
    # ------------------------------------------------------------------
    @app.route("/tests/<test_id>/report", methods=["GET"])
    def route_tests_report(test_id: str) -> Any:
        with _test_runs_lock:
            run = _test_runs.get(test_id)
        if not run:
            return _err(f"Test run '{test_id}' not found", 404)

        output_dir = run.get("output_dir")
        if not output_dir:
            return _err("Test run has no output directory yet (still running?)")

        out_path = Path(output_dir)
        fmt = request.args.get("format", "json")

        if run.get("kind") == "fleet":
            # Fleet report
            report_file = out_path / "fleet-report.json"
        else:
            # Single target — look for report.json in the output dir
            report_file = out_path / "report.json"

        if fmt == "json":
            if report_file.is_file():
                import json as _json
                data = _json.loads(report_file.read_text(encoding="utf-8"))
                return jsonify({"ok": True, "test_id": test_id, "report": data})
            # Fallback: return summary from the test run metadata
            summary = run.get("summary")
            if summary:
                return jsonify({"ok": True, "test_id": test_id, "report": summary})
            return _err("Report not available yet")

        # For html/markdown, look for the file
        is_fleet = run.get("kind") == "fleet"
        ext_map = {
            "html": "fleet-report.html" if is_fleet else "report.html",
            "markdown": "fleet-report.md" if is_fleet else "report.md",
        }
        filename = ext_map.get(fmt)
        if not filename:
            return _err(f"Unknown format '{fmt}'. Expected: json, html, markdown")

        report_path = out_path / filename
        if not report_path.is_file():
            return _err(f"{filename} not found in output directory")

        from flask import send_file
        return send_file(str(report_path))

    # ==================================================================
    # Dashboard — simple HTML status page
    # ==================================================================

    # ------------------------------------------------------------------
    # GET /dashboard — HTML page showing pods, tests, jobs
    # ------------------------------------------------------------------
    @app.route("/dashboard", methods=["GET"])
    def route_dashboard() -> Any:
        from flask import render_template_string

        # Gather data
        try:
            provider = _get_runpod_provider()
            from comfy_runner.hosted.config import list_pod_records
            pods_data = []
            records = list_pod_records("runpod")
            live_pods = provider.list_pods()
            live_map = {}
            for p in live_pods:
                live_map[p.name] = p
                live_map[p.id] = p
            for pname, rec in records.items():
                live = live_map.get(pname) or live_map.get(rec.get("id", ""))
                ts_url = provider.get_pod_tailscale_url(pname, port=9189)
                pods_data.append({
                    "name": pname,
                    "id": rec.get("id", ""),
                    "status": live.status if live else "UNKNOWN",
                    "gpu_type": (live.gpu_type if live else "") or rec.get("gpu_type", ""),
                    "cost_per_hr": live.cost_per_hr if live else 0,
                    "server_url": ts_url or "",
                })
        except Exception:
            pods_data = []

        test_runs = _list_test_runs(limit=20)
        active_jobs = _jobs.list_active()

        html = _DASHBOARD_HTML
        return render_template_string(
            html,
            pods=pods_data,
            test_runs=test_runs,
            jobs=active_jobs,
        )

    # ------------------------------------------------------------------
    # POST /self-update — git pull and restart the server process
    # ------------------------------------------------------------------
    @app.route("/self-update", methods=["POST"])
    def route_self_update() -> Any:
        import os
        import subprocess
        import sys

        repo_dir = Path(__file__).resolve().parent.parent
        body = request.get_json(silent=True) or {}
        force = body.get("force", False)

        # git pull (or force-reset)
        try:
            # Always fetch first
            subprocess.run(
                ["git", "fetch", "--all"],
                cwd=str(repo_dir),
                capture_output=True, text=True, timeout=30,
            )
            if force:
                result = subprocess.run(
                    ["git", "reset", "--hard", "origin/main"],
                    cwd=str(repo_dir),
                    capture_output=True, text=True, timeout=30,
                )
            else:
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

        # Install any new/changed dependencies before restarting
        req_file = repo_dir / "requirements.txt"
        deps_output = ""
        if req_file.exists():
            try:
                pip_result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)],
                    cwd=str(repo_dir),
                    capture_output=True, text=True, timeout=120,
                )
                deps_output = pip_result.stdout.strip()
                if pip_result.returncode != 0:
                    log.warning("pip install failed during self-update: %s", pip_result.stderr.strip())
            except Exception as e:
                log.warning("pip install failed during self-update: %s", e)

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
