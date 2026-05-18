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
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("comfy-runner-server")

_tailscale_mode = False
_tunnels_enabled = False


# ---------------------------------------------------------------------------
# Build-mode params on the deploy endpoint
# ---------------------------------------------------------------------------
#
# These keys only make sense for the ad-hoc build flow (they choose Python
# version, PyTorch build, etc.). When any of them is present in a deploy body
# we implicitly enable ``build: true`` for auto-init.
#
# ``comfyui_ref`` is intentionally NOT in this list: it works in both the
# prebuilt and ad-hoc-build flows, so we forward it separately regardless of
# build mode.
_BUILD_TRIGGER_KEYS = (
    "python_version", "pbs_release", "gpu",
    "cuda_tag", "torch_version", "torch_spec",
    "torch_index_url",
)


def _extract_build_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    """Decide build mode + extract build-specific kwargs from a deploy body.

    Build mode is active when:
      - ``body["build"]`` is truthy, OR
      - ``build`` is absent/None AND any of ``_BUILD_TRIGGER_KEYS`` is present.

    An explicit ``"build": False`` always wins — it suppresses the implicit
    trigger so callers can defensively pass build-related params alongside
    ``build: false`` to mean "definitely don't build".

    Returns ``{}`` when build mode is not active, otherwise a dict starting
    with ``{"build": True}`` plus each trigger key present in *body*.
    """
    if body.get("build") is False:
        return {}
    triggered = bool(body.get("build")) or any(k in body for k in _BUILD_TRIGGER_KEYS)
    if not triggered:
        return {}
    out: dict[str, Any] = {"build": True}
    for k in _BUILD_TRIGGER_KEYS:
        if k in body:
            out[k] = body[k]
    return out


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
# Test run index — tracks test runs separately from generic jobs.
#
# Persisted to disk via comfy_runner_server.persistence so the dashboard
# "Recent Test Runs" section survives server restart, self-update, and
# crashes.  Writes are debounced (default 2 s trailing) to coalesce
# bursty fleet-CI updates.
# ---------------------------------------------------------------------------

_test_runs_lock = threading.Lock()
_test_runs: dict[str, dict[str, Any]] = {}

# Bumped from 200 → 1000 now that the registry persists. Beyond size
# also evict anything older than _MAX_TEST_RUN_AGE_S so the file does
# not grow unboundedly when a long-lived server keeps churning runs.
_MAX_TEST_RUNS = 1000
_MAX_TEST_RUN_AGE_S = 30 * 24 * 3600  # 30 days


_saver_instance: Any = None
# Guards construction of ``_saver_instance``.  Without it concurrent
# first-time callers from different request threads could each see
# ``_saver_instance is None`` and spin up parallel DebouncedSaver
# instances (and orphan background timer threads).
_saver_instance_lock = threading.Lock()


def _persistence_saver() -> Any:
    """Return the lazily-constructed DebouncedSaver for ``_test_runs``.

    Defined as a function (not a module-global) so test code that
    resets the cached instance via ``srv._saver_instance = None`` lands
    on the next call.  Double-checked locking keeps the fast path
    lock-free after the saver is built.
    """
    global _saver_instance
    if _saver_instance is not None:
        return _saver_instance
    with _saver_instance_lock:
        if _saver_instance is None:
            from comfy_runner_server import persistence

            def _snapshot() -> dict[str, dict[str, Any]]:
                with _test_runs_lock:
                    # Deep-copy the inner dicts so the background save
                    # thread cannot observe a mutation mid-serialize.
                    import copy as _copy
                    return {k: _copy.deepcopy(v) for k, v in _test_runs.items()}

            _saver_instance = persistence.DebouncedSaver(snapshot=_snapshot)
    return _saver_instance


def _schedule_test_runs_save() -> None:
    """Schedule a debounced persistence write (no-op if disabled)."""
    import os
    if os.environ.get("COMFY_RUNNER_DISABLE_TEST_RUN_PERSISTENCE"):
        return
    try:
        _persistence_saver().schedule()
    except Exception as e:  # noqa: BLE001
        log.debug("test-run persistence schedule failed: %s", e)


def _register_test_run(job_id: str, meta: dict[str, Any]) -> None:
    """Register a test run keyed by its job_id."""
    with _test_runs_lock:
        _test_runs[job_id] = {
            "id": job_id,
            "created_at": time.time(),
            **meta,
        }
    _schedule_test_runs_save()


def _finish_test_run(job_id: str, updates: dict[str, Any], status: str = "done") -> None:
    """Update a test run with final results and persist status."""
    changed = False
    with _test_runs_lock:
        run = _test_runs.get(job_id)
        if run:
            run.update(updates)
            run["status"] = status
            run["finished_at"] = time.time()
            changed = True
    if changed:
        _schedule_test_runs_save()


def _gc_test_runs() -> None:
    """Evict expired and over-cap test runs (caller must hold lock).

    Eviction order:
    1. Drop entries older than ``_MAX_TEST_RUN_AGE_S`` regardless of count.
    2. If still over ``_MAX_TEST_RUNS``, drop the oldest by ``created_at``.

    Triggers a debounced persistence save only if something was removed.
    """
    removed = False
    now = time.time()
    # Age-based expiry first.
    expired = [
        k for k, v in _test_runs.items()
        if now - v.get("created_at", 0) > _MAX_TEST_RUN_AGE_S
    ]
    for k in expired:
        del _test_runs[k]
        removed = True
    # Size-based cap.
    if len(_test_runs) > _MAX_TEST_RUNS:
        by_time = sorted(_test_runs.keys(), key=lambda k: _test_runs[k].get("created_at", 0))
        to_remove = len(_test_runs) - _MAX_TEST_RUNS
        for k in by_time[:to_remove]:
            del _test_runs[k]
            removed = True
    if removed:
        # Caller holds _test_runs_lock; schedule is lock-free so it's safe.
        _schedule_test_runs_save()


def _load_persisted_test_runs() -> None:
    """Populate ``_test_runs`` from disk.  Called once at server startup."""
    import os
    if os.environ.get("COMFY_RUNNER_DISABLE_TEST_RUN_PERSISTENCE"):
        return
    try:
        from comfy_runner_server import persistence
        persisted = persistence.load_test_runs()
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to load persisted test runs: %s", e)
        return
    if not persisted:
        return
    with _test_runs_lock:
        # Existing in-memory entries (rare — only if a test seeded data
        # before startup ran) win over persisted ones.
        for k, v in persisted.items():
            _test_runs.setdefault(k, v)
        _gc_test_runs()
    log.info("Loaded %d persisted test run(s) from disk", len(persisted))


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


# ---------------------------------------------------------------------------
# Pod activity tracking + idle reaper
#
# PR-review pods (records with ``purpose == "pr"``) are stopped after they
# go idle for ``idle_timeout_s`` seconds (default ``DEFAULT_IDLE_TIMEOUT_S``).
# Activity is recorded in the pod record's ``last_active_at`` (epoch
# seconds, UTC).  Any meaningful interaction with the pod via the central
# server should call ``_touch_pod_activity(name)``.
# ---------------------------------------------------------------------------

DEFAULT_IDLE_TIMEOUT_S = 600
# TTL for ``purpose='test'`` ephemeral pods. Anything older than this
# is force-terminated by the reaper, since these pods are meant to be
# torn down at the end of ``run_on_runpod`` -- if one is still around
# past the TTL, the auto-terminate hook silently failed and we don't
# want a forgotten pod to keep burning GPU.
DEFAULT_TEST_POD_TTL_S = 7200
_REAPER_INTERVAL_S = 60

# Default wall-clock budget for ``_wait_for_pod_server`` polling.
# Resumed RunPod pods on secure-cloud L40S routinely take 5-8 minutes
# to register their Tailscale hostname and start serving HTTP, so the
# old 300s default produced spurious failures during fleet-ci wakes.
# Configurable per-call via the ``pod_ready_timeout_s`` body parameter
# on ``/pods/create-ci-runner`` and ``/tests/fleet-ci``.
DEFAULT_POD_READY_TIMEOUT_S = 600

# RunPod ``POST /pods/{id}/start`` failure messages that we treat as
# transient: the host machine that previously ran the suspended pod
# may have lost free GPU capacity, in which case waiting briefly and
# retrying often lets RunPod re-place the resume on a new host. We
# only retry on these specific substrings -- any other start failure
# (auth, bad pod id, terminated pod) propagates immediately so the
# operator can intervene.
_TRANSIENT_START_ERROR_SUBSTRINGS = (
    "not enough free GPUs",
    "Failed to resume pod",
)
DEFAULT_START_RETRIES = 5
DEFAULT_START_RETRY_DELAY_S = 30

# Orphan reconciliation -- safety net for pods that exist on RunPod but
# have NO entry in the central pod registry. Without reconciliation
# these pods are invisible to the existing reaper (which iterates the
# registry only) and bill indefinitely. Real incident: 5 5090 pods
# leaked for ~10h ($50) because ``_dispatch_auto_stop`` removed their
# registry records on a transient ``terminate_pod`` failure, then no
# one could see them anymore.
#
# To avoid blast-radius the reconciler is deliberately conservative:
#   * Only touches pods whose name matches the auto-generated
#     ``test-<unix_ts>`` pattern produced by ``run_on_runpod``. This
#     deliberately excludes ``persistent``, ``pr``, ``ci-runner``, or
#     hand-named pods -- if a user named their dev pod ``test-foo`` it
#     won't match (digits-only suffix).
#   * Only acts on pods older than ``DEFAULT_TEST_POD_TTL_S``. The
#     in-name timestamp is the source of truth (it's how the test
#     runner names them); RunPod's ``createdAt`` is a fallback for
#     defense-in-depth.
#   * Skips any pod that *does* have a registry record -- that pod is
#     handled by the per-purpose branches above.
#   * Logs every reconciler-triggered termination at WARNING so the
#     operator can audit any auto-cleanup after the fact.
_ORPHAN_TEST_POD_PATTERN = re.compile(r"^test-(\d{9,11})$")


def _is_transient_start_error(exc: BaseException) -> bool:
    """Return True if *exc* matches a known transient RunPod resume error."""
    msg = str(exc)
    return any(sub in msg for sub in _TRANSIENT_START_ERROR_SUBSTRINGS)


def _start_pod_with_retry(
    provider: Any,
    pod_id: str,
    send_output: Callable[[str], None] | None = None,
    max_retries: int = DEFAULT_START_RETRIES,
    retry_delay_s: int = DEFAULT_START_RETRY_DELAY_S,
) -> Any:
    """Call ``provider.start_pod(pod_id)`` with retries on transient errors.

    RunPod's ``POST /pods/{id}/start`` can fail with "There are not
    enough free GPUs on the host machine to start this pod" or "Failed
    to resume pod" when the host that previously ran the suspended pod
    no longer has free capacity. These are transient -- a brief wait
    usually lets RunPod find capacity elsewhere -- so we retry up to
    ``max_retries`` times with a fixed ``retry_delay_s`` delay between
    attempts. Any non-matching error propagates immediately. Returns
    whatever ``provider.start_pod`` returns on success.
    """
    out = send_output or (lambda _: None)
    last_exc: BaseException | None = None
    attempts = max(1, max_retries + 1)
    for attempt in range(attempts):
        try:
            return provider.start_pod(pod_id)
        except Exception as exc:
            last_exc = exc
            if not _is_transient_start_error(exc):
                raise
            if attempt + 1 >= attempts:
                break
            out(
                f"Pod start failed transiently (attempt "
                f"{attempt + 1}/{attempts}): {exc}\n"
                f"Retrying in {retry_delay_s}s...\n"
            )
            time.sleep(retry_delay_s)
    assert last_exc is not None
    raise last_exc

_reaper_lock = threading.Lock()
_reaper_started = False


def _touch_pod_activity(name: str) -> None:
    """Mark a pod as active by stamping ``last_active_at`` on its record.

    Also clears any reaper-stamped ``status_hint`` (e.g. ``stopped_idle``,
    ``exited``) since the pod is being interacted with again.
    """
    from comfy_runner.hosted.config import update_pod_record

    def _apply(rec: dict[str, Any] | None) -> dict[str, Any] | None:
        if rec is None:
            return None
        rec["last_active_at"] = int(time.time())
        rec.pop("status_hint", None)
        return rec

    update_pod_record("runpod", name, _apply)


def _idle_seconds_remaining(rec: dict[str, Any]) -> int | None:
    """Return seconds remaining before *rec* is eligible for idle-stop, or
    ``None`` if the pod is not subject to the idle-stop reaper.

    Applies to ``purpose='pr'`` (PR-review pods) and ``purpose='ci-runner'``
    (long-lived test runners that suspend between CI invocations). Both
    are *stopped*, never terminated, by the reaper.
    """
    if rec.get("purpose") not in ("pr", "ci-runner"):
        return None
    timeout = int(rec.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S)
    last = rec.get("last_active_at")
    if not last:
        return timeout
    return max(0, timeout - int(time.time() - int(last)))


def _test_pod_ttl_remaining(rec: dict[str, Any]) -> int | None:
    """Return seconds remaining before *rec* is eligible for force-terminate,
    or ``None`` if the pod is not a ``purpose='test'`` ephemeral pod.

    ``purpose='test'`` pods are meant to be terminated by ``run_on_runpod``
    when the suite finishes. If one survives past ``ttl_s`` (default
    ``DEFAULT_TEST_POD_TTL_S``) it's an orphan -- the auto-terminate hook
    failed -- and the reaper destroys it. We use ``last_active_at`` if
    present (set when the pod is touched/created), else ``created_at``,
    else assume "now" so a pod with no timestamps gets a fresh TTL.
    """
    if rec.get("purpose") != "test":
        return None
    ttl = int(rec.get("ttl_s") or DEFAULT_TEST_POD_TTL_S)
    last = rec.get("last_active_at") or rec.get("created_at")
    if not last:
        return ttl
    return max(0, ttl - int(time.time() - int(last)))


# Pod purposes for which ``_ensure_pod_running`` may *terminate +
# recreate* a stuck pod (after start retries are exhausted with a
# transient capacity error) when the pod has a ``volume_id`` on its
# record. The recreate gives RunPod a chance to place the pod on a
# fresh host, and the named network volume preserves ``/workspace``
# (cached models, deployments) across the cycle.
#
# Restricted to ``ci-runner`` for now -- ``persistent`` pods may have
# unsaved dev state on the ephemeral container disk, and ``pr`` pods
# don't currently set up named volumes.
_RECREATE_ON_STUCK_PURPOSES = ("ci-runner",)


def _recreate_pod_for_capacity(
    name: str,
    send_output: Callable[[str], None] | None = None,
) -> Any:
    """Terminate + recreate a pod with the same identity and reattach
    its network volume so RunPod re-places it on a fresh host.

    Used as a last-resort fallback by ``_ensure_pod_running`` when the
    underlying ``_start_pod_with_retry`` exhausts its retries on
    transient capacity errors -- the host that previously ran the
    suspended pod has lost its free GPUs, and RunPod's resume path
    keeps re-trying the same host. A full terminate forces RunPod to
    pick a new host on the next ``create_pod`` call.

    Only safe when the pod has a ``volume_id`` on its record. The
    volume preserves ``/workspace`` (where ``COMFY_RUNNER_HOME`` lives,
    so installations and downloaded models survive). Without a volume
    the pod's container disk would be wiped and we'd silently lose the
    cached state -- callers must enforce this precondition before
    calling.

    Updates the pod record in-place (new pod id, refreshed metadata)
    and returns the new ``PodInfo``.
    """
    from comfy_runner.hosted.config import (
        get_pod_record,
        update_pod_record,
    )

    out = send_output or (lambda _: None)
    rec = get_pod_record("runpod", name)
    if not rec:
        raise RuntimeError(f"Pod '{name}' not found in config")
    volume_id = rec.get("volume_id")
    if not volume_id:
        raise RuntimeError(
            f"Pod '{name}' has no volume_id on its record; "
            f"cannot safely terminate + recreate (would wipe "
            f"/workspace cache)"
        )
    provider = _get_runpod_provider()
    old_id = rec["id"]
    out(
        f"Terminating pod '{name}' (id: {old_id}) for fresh host "
        f"placement -- network volume '{volume_id}' preserves "
        f"/workspace data...\n"
    )
    try:
        provider.terminate_pod(old_id)
    except Exception as e:
        # Pod may already be gone; continue. Worst case we leak a
        # billed pod -- the orphan reconciler will clean it up.
        out(f"  terminate raised: {e} (continuing)\n")

    out(f"Recreating pod '{name}' on a fresh host...\n")
    new_pod = provider.create_pod(
        name=name,
        gpu_type=rec.get("gpu_type"),
        image=rec.get("image"),
        volume_id=volume_id,
        container_disk_gb=rec.get("container_disk_gb"),
        datacenter=rec.get("datacenter") or None,
        cloud_type=rec.get("cloud_type"),
    )

    def _apply(r: dict[str, Any] | None) -> dict[str, Any] | None:
        if r is None:
            return None
        r["id"] = new_pod.id
        if new_pod.gpu_type:
            r["gpu_type"] = new_pod.gpu_type
        if new_pod.datacenter:
            r["datacenter"] = new_pod.datacenter
        if new_pod.image:
            r["image"] = new_pod.image
        r["last_active_at"] = int(time.time())
        r.pop("status_hint", None)
        return r

    update_pod_record("runpod", name, _apply)
    out(
        f"Recreated (id: {new_pod.id}, datacenter: "
        f"{new_pod.datacenter or '(unknown)'})\n"
    )
    return new_pod


def _ensure_pod_running(
    name: str,
    send_output: Callable[[str], None] | None = None,
    wait_ready: bool = True,
    pod_ready_timeout_s: int | None = None,
    start_retries: int = DEFAULT_START_RETRIES,
    start_retry_delay_s: int = DEFAULT_START_RETRY_DELAY_S,
    recreate_on_stuck: bool = True,
) -> str:
    """Start a stopped/exited pod and (optionally) wait for its server.

    Updates ``last_active_at`` on the record. Returns the resolved
    server URL. Raises ``RuntimeError`` if the pod cannot be reached.

    ``pod_ready_timeout_s`` overrides the default
    ``DEFAULT_POD_READY_TIMEOUT_S`` budget for the wait-ready poll
    loop. ``start_retries`` / ``start_retry_delay_s`` configure
    retries on transient RunPod resume errors (capacity-exhausted
    host re-placement); see ``_start_pod_with_retry``.

    If ``recreate_on_stuck`` is True (the default) and start retries
    *still* exhaust with a transient capacity error AND the pod has a
    ``volume_id`` on its record AND its ``purpose`` is in
    ``_RECREATE_ON_STUCK_PURPOSES``, the pod is terminated and recreated
    on a fresh host (the network volume preserves ``/workspace``).
    See ``_recreate_pod_for_capacity``.
    """
    from comfy_runner.hosted.config import get_pod_record
    out = send_output or (lambda _: None)
    rec = get_pod_record("runpod", name)
    if not rec:
        raise RuntimeError(f"Pod '{name}' not found in config")
    provider = _get_runpod_provider()
    pod_id = rec["id"]
    pod = provider.get_pod(pod_id)
    if not pod or pod.status in ("TERMINATED", "EXITED"):
        if not pod:
            raise RuntimeError(f"Pod '{name}' is gone on RunPod")
        # EXITED pods can be started by RunPod's start API; fall through.
    if pod and pod.status != "RUNNING":
        out(f"Pod '{name}' is {pod.status}, starting...\n")
        try:
            _start_pod_with_retry(
                provider, pod_id, send_output=out,
                max_retries=start_retries,
                retry_delay_s=start_retry_delay_s,
            )
        except Exception as e:
            should_recreate = (
                recreate_on_stuck
                and _is_transient_start_error(e)
                and rec.get("volume_id")
                and rec.get("purpose") in _RECREATE_ON_STUCK_PURPOSES
            )
            if not should_recreate:
                raise
            out(
                f"Start retries exhausted ({e}); pod has a network "
                f"volume -- attempting terminate + recreate for fresh "
                f"host placement...\n"
            )
            _recreate_pod_for_capacity(name, send_output=out)
    _touch_pod_activity(name)
    if wait_ready:
        timeout = pod_ready_timeout_s or DEFAULT_POD_READY_TIMEOUT_S
        return _wait_for_pod_server(
            name, timeout=timeout, send_output=send_output,
        )
    return _get_pod_server_url(name, raise_on_error=False) or ""


def _idle_reaper_iteration() -> dict[str, Any]:
    """Run a single reaper sweep. Returns a summary dict (also useful in tests).

    Three behaviors based on the pod's ``purpose`` tag:

    * ``purpose='pr'`` -- stop the pod if it's RUNNING and past its
      idle timeout. Never terminates.
    * ``purpose='ci-runner'`` -- same as ``'pr'`` (stop only). These are
      long-lived test runners that suspend between CI invocations to
      preserve the cached models on their container disk.
    * ``purpose='test'`` -- terminate the pod (regardless of status) if
      its TTL has elapsed. These are ephemeral pods spawned by
      ``run_on_runpod`` whose auto-terminate hook silently failed.

    Skips pods whose lock is currently held (i.e. an operation is in
    flight).
    """
    from comfy_runner.hosted.config import (
        list_pod_records,
        remove_pod_record,
        update_pod_record,
    )

    summary: dict[str, Any] = {
        "checked": 0,
        "stopped": [],
        "terminated": [],
        "skipped": [],
    }

    try:
        provider = _get_runpod_provider()
    except Exception as e:
        log.debug("idle reaper: provider unavailable (%s); skipping sweep", e)
        return summary

    records = list_pod_records("runpod")
    for name, rec in records.items():
        purpose = rec.get("purpose")
        if purpose in ("pr", "ci-runner"):
            summary["checked"] += 1
            remaining = _idle_seconds_remaining(rec)
            if remaining is None or remaining > 0:
                summary["skipped"].append({"name": name, "reason": "active",
                                            "remaining_s": remaining})
                continue
            lock = _get_pod_lock(name)
            if not lock.acquire(blocking=False):
                summary["skipped"].append({"name": name, "reason": "busy"})
                continue
            try:
                pod_id = rec.get("id")
                if not pod_id:
                    continue
                try:
                    pod = provider.get_pod(pod_id)
                except Exception as e:
                    log.warning("idle reaper: get_pod %s failed: %s", name, e)
                    summary["skipped"].append({"name": name, "reason": f"api: {e}"})
                    continue
                if not pod or pod.status != "RUNNING":
                    if pod:
                        pod_status_lower = pod.status.lower()

                        def _sync_hint(r: dict[str, Any] | None,
                                       _hint: str = pod_status_lower) -> dict[str, Any] | None:
                            if r is None:
                                return None
                            if r.get("status_hint") != "stopped_idle":
                                r["status_hint"] = _hint
                            return r
                        update_pod_record("runpod", name, _sync_hint)
                    summary["skipped"].append({
                        "name": name,
                        "reason": f"not running ({pod.status if pod else 'gone'})",
                    })
                    continue
                try:
                    provider.stop_pod(pod_id)
                except Exception as e:
                    log.warning("idle reaper: stop_pod %s failed: %s", name, e)
                    summary["skipped"].append({"name": name, "reason": f"stop: {e}"})
                    continue

                def _mark_idle(r: dict[str, Any] | None) -> dict[str, Any] | None:
                    if r is None:
                        return None
                    r["status_hint"] = "stopped_idle"
                    r["stopped_at"] = int(time.time())
                    return r
                update_pod_record("runpod", name, _mark_idle)
                summary["stopped"].append({
                    "name": name, "id": pod_id, "purpose": purpose,
                })
                log.info(
                    "idle reaper: stopped %s pod '%s' (idle > %ss)",
                    purpose, name,
                    rec.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S,
                )
            finally:
                lock.release()
        elif purpose == "test":
            summary["checked"] += 1
            remaining = _test_pod_ttl_remaining(rec)
            if remaining is None or remaining > 0:
                summary["skipped"].append({"name": name, "reason": "active",
                                            "remaining_s": remaining})
                continue
            lock = _get_pod_lock(name)
            if not lock.acquire(blocking=False):
                summary["skipped"].append({"name": name, "reason": "busy"})
                continue
            try:
                pod_id = rec.get("id")
                if not pod_id:
                    # No id -- just drop the stale record.
                    try:
                        remove_pod_record("runpod", name)
                    except Exception:
                        pass
                    summary["terminated"].append({"name": name, "id": None})
                    continue
                try:
                    provider.terminate_pod(pod_id)
                except Exception as e:
                    log.warning(
                        "idle reaper: terminate_pod %s failed: %s",
                        name, e,
                    )
                    summary["skipped"].append({
                        "name": name, "reason": f"terminate: {e}",
                    })
                    continue
                try:
                    remove_pod_record("runpod", name)
                except Exception as e:
                    log.warning(
                        "idle reaper: remove_pod_record %s failed: %s",
                        name, e,
                    )
                summary["terminated"].append({
                    "name": name, "id": pod_id, "purpose": "test",
                })
                log.info(
                    "idle reaper: terminated orphan test pod '%s' "
                    "(ttl > %ss, auto-terminate hook failed)",
                    name, rec.get("ttl_s") or DEFAULT_TEST_POD_TTL_S,
                )
            finally:
                lock.release()

    # ── Orphan reconciliation pass ──────────────────────────────────
    # Belt-and-suspenders defense against pods that escaped the
    # registry (Bug A symptom in this incident, but any future bug of
    # the same shape is also caught). See ``_ORPHAN_TEST_POD_PATTERN``
    # docstring above for the safety constraints.
    _orphan_pod_reconciliation(provider, records, summary)
    return summary


def _orphan_pod_reconciliation(
    provider: Any,
    records: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    """Find and terminate test pods on RunPod that have no registry record.

    Iterates ``provider.list_pods()`` and matches any pod whose name
    fits ``_ORPHAN_TEST_POD_PATTERN`` (``test-<unix_ts>``) and that:

    * is NOT already in *records* (the per-purpose loop above handles
      tracked pods), and
    * has an in-name timestamp older than ``now - DEFAULT_TEST_POD_TTL_S``

    Such a pod is a recoverable orphan: the test runner that created
    it crashed/leaked before its auto-terminate hook could run, OR a
    bug elsewhere stripped the registry record. Either way, no human
    is going to discover it -- the existing reaper iterates the
    registry only and would never see it. This function calls
    ``provider.terminate_pod`` and logs at WARNING so the operator
    can audit.

    Mutates *summary* in place: appends entries to
    ``summary["terminated"]`` with ``"reason": "orphan_no_record"``,
    or to ``summary["skipped"]`` on transient API errors. Counts
    every checked candidate in ``summary["checked"]``.
    """
    try:
        actual_pods = provider.list_pods()
    except Exception as e:
        log.warning(
            "orphan reconciler: provider.list_pods failed: %s "
            "(skipping orphan sweep this iteration)",
            e,
        )
        return

    now = int(time.time())
    cutoff_age_s = DEFAULT_TEST_POD_TTL_S
    for pod in actual_pods:
        name = getattr(pod, "name", "") or ""
        m = _ORPHAN_TEST_POD_PATTERN.match(name)
        if not m:
            continue  # not an auto-generated test pod name -- ignore
        if name in records:
            continue  # tracked; the per-purpose loop already handled it
        try:
            ts = int(m.group(1))
        except ValueError:
            continue
        age_s = now - ts
        if age_s < cutoff_age_s:
            # Young enough that an in-flight test runner might still
            # be racing to register it. Wait it out.
            summary["skipped"].append({
                "name": name,
                "reason": f"orphan_too_young (age {age_s}s < {cutoff_age_s}s)",
            })
            continue

        summary["checked"] += 1
        pod_id = getattr(pod, "id", "") or ""
        if not pod_id:
            log.warning(
                "orphan reconciler: candidate '%s' has no id; skipping",
                name,
            )
            continue
        try:
            provider.terminate_pod(pod_id)
        except Exception as e:
            log.warning(
                "orphan reconciler: terminate_pod %s (id=%s) failed: %s "
                "(will retry next iteration)",
                name, pod_id, e,
            )
            summary["skipped"].append({
                "name": name,
                "reason": f"orphan_terminate_failed: {e}",
            })
            continue

        summary["terminated"].append({
            "name": name,
            "id": pod_id,
            "reason": "orphan_no_record",
            "age_s": age_s,
        })
        log.warning(
            "orphan reconciler: terminated registry-less test pod "
            "'%s' (id=%s, age %ss). The pod was created by a test "
            "runner whose auto-terminate hook failed AND whose "
            "registry record was lost; investigate the upstream bug.",
            name, pod_id, age_s,
        )


def _idle_reaper_loop(stop_event: threading.Event) -> None:
    """Background loop that runs ``_idle_reaper_iteration`` periodically."""
    while not stop_event.wait(_REAPER_INTERVAL_S):
        try:
            _idle_reaper_iteration()
        except Exception as e:
            log.warning("idle reaper: iteration failed: %s", e)


def _start_idle_reaper_once() -> None:
    """Start the idle reaper background thread (idempotent)."""
    global _reaper_started
    with _reaper_lock:
        if _reaper_started:
            return
        stop_event = threading.Event()
        t = threading.Thread(
            target=_idle_reaper_loop,
            args=(stop_event,),
            name="comfy-runner-idle-reaper",
            daemon=True,
        )
        t.start()
        _reaper_started = True
        log.info("Idle reaper started (interval=%ss, default timeout=%ss)",
                 _REAPER_INTERVAL_S, DEFAULT_IDLE_TIMEOUT_S)


# Cache the Tailscale device list to avoid hammering the API when
# resolving many pod URLs in a single dashboard / list request.
_TS_DEVICES_CACHE: dict[str, Any] = {"at": 0.0, "devices": []}
_TS_DEVICES_TTL = 30.0
_TS_DEVICES_LOCK = threading.Lock()


def _list_tailscale_devices(force: bool = False) -> list[dict[str, Any]]:
    """Return cached Tailscale device list, refreshing if stale.

    Returns ``[]`` if the API key/tailnet are not configured or the
    request fails. Cached for ``_TS_DEVICES_TTL`` seconds.
    """
    from comfy_runner.hosted.config import (
        get_tailscale_api_key,
        get_tailscale_tailnet,
    )
    api_key = get_tailscale_api_key()
    tailnet = get_tailscale_tailnet()
    if not api_key or not tailnet:
        return []
    now = time.monotonic()
    with _TS_DEVICES_LOCK:
        if not force and now - _TS_DEVICES_CACHE["at"] < _TS_DEVICES_TTL:
            return list(_TS_DEVICES_CACHE["devices"])
    try:
        import requests as _requests
        resp = _requests.get(
            f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if not resp.ok:
            log.warning("Tailscale device list failed: HTTP %s", resp.status_code)
            return []
        devices = resp.json().get("devices", []) or []
    except Exception as e:
        log.warning("Tailscale device list failed: %s", e)
        return []
    with _TS_DEVICES_LOCK:
        _TS_DEVICES_CACHE["at"] = now
        _TS_DEVICES_CACHE["devices"] = devices
    return list(devices)


def _resolve_tailscale_hostname(pod_name: str, force: bool = False) -> str | None:
    """Find the actual host (IPv4 or MagicDNS name) for a pod via the Tailscale API.

    Handles two failure modes:

    * **Suffix drift** – the pod's hostname got auto-suffixed
      (e.g. ``comfy-foo-1`` instead of ``comfy-foo``) because a stale
      device with the same name was still online when the new pod joined.
    * **Duplicate hostnames** – on tailnets that *don't* enforce unique
      hostnames, the new pod and the stale (offline) one coexist with
      the *same* hostname. MagicDNS would then ambiguously resolve to
      either device's IP, often the dead one.

    To dodge MagicDNS ambiguity entirely we return the chosen device's
    Tailscale IP (the first IPv4 from its ``addresses`` field) when
    available, falling back to the MagicDNS FQDN. The chosen device is
    always the most-recently-seen match — i.e. the live pod.

    Returns the host string (e.g. ``"100.86.23.124"`` or
    ``"comfy-foo-1.tailnet.ts.net"``), or ``None`` if no matching
    device is found / API not configured.
    """
    import re
    expected = f"comfy-{pod_name}"
    devices = _list_tailscale_devices(force=force)
    if not devices:
        return None
    suffix_re = re.compile(r"^" + re.escape(expected) + r"(-\d+)?$")
    matches: list[dict[str, Any]] = []
    for d in devices:
        host = d.get("hostname", "") or ""
        if not host:
            fqdn = d.get("name", "") or ""
            host = fqdn.split(".", 1)[0]
        if suffix_re.match(host):
            matches.append(d)
    if not matches:
        return None
    # Most-recently-seen first; ISO 8601 timestamps sort correctly as strings.
    matches.sort(
        key=lambda d: (d.get("lastSeen", ""), d.get("created", "")),
        reverse=True,
    )
    chosen = matches[0]
    # Prefer the device's IPv4 address (bypasses MagicDNS ambiguity).
    for addr in chosen.get("addresses", []) or []:
        if addr and "." in addr and ":" not in addr:
            return addr
    # Fallback to the MagicDNS FQDN.
    return chosen.get("name") or None


def _get_pod_server_url(
    pod_name: str,
    port: int = 9189,
    raise_on_error: bool = True,
) -> str | None:
    """Resolve the comfy-runner server URL for a named pod.

    This is the single source of truth for pod URL resolution. It tries:
      1. The actual host (IPv4 or MagicDNS name) from the Tailscale REST
         API — handles hostname drift like ``comfy-foo-1`` and
         duplicate-hostname ambiguity by always picking the
         most-recently-seen device.
      2. The expected unsuffixed Tailscale MagicDNS URL
         (``http://comfy-<name>.<tailnet>:<port>``) for tailnets
         where API access isn't configured.

    The RunPod public proxy is intentionally NOT a fallback: pods no
    longer expose public ports, so it would always be unreachable.

    When ``raise_on_error`` is False, returns ``None`` if nothing
    could be resolved instead of raising.
    """
    # 1. Tailscale API lookup — finds drifted hostnames and live devices
    host = _resolve_tailscale_hostname(pod_name)
    if host:
        return f"http://{host}:{port}"

    # 2. Expected (unsuffixed) Tailscale MagicDNS URL
    try:
        from comfy_runner.hosted.runpod_provider import RunPodProvider
        provider = RunPodProvider()
        url = provider.get_pod_tailscale_url(pod_name, port=port)
        if url:
            return url
    except Exception:
        pass

    if raise_on_error:
        raise RuntimeError(
            f"Cannot resolve server URL for pod '{pod_name}'. "
            f"Tailscale must be configured (auth key) and the pod must "
            f"be on the tailnet."
        )
    return None


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


def _wait_for_pod_server(
    pod_name: str,
    timeout: int = DEFAULT_POD_READY_TIMEOUT_S,
    poll_interval: int = 10,
    send_output: Callable[[str], None] | None = None,
) -> str:
    """Poll a pod's comfy-runner server until it responds.

    Re-resolves the pod URL on every iteration so that a drifted
    Tailscale hostname (e.g. ``comfy-foo-1``) registered after pod
    boot is picked up automatically. Returns the URL that finally
    responded.
    """
    import requests as _requests
    from comfy_runner.hosted.config import (
        backfill_pod_metadata,
        get_pod_record,
    )
    out = send_output or (lambda _: None)
    deadline = time.monotonic() + timeout
    last_url: str | None = None
    last_logged_url: str | None = None
    force_refresh = False
    while time.monotonic() < deadline:
        # Force a Tailscale device-list refresh periodically so a
        # newly-registered (suffixed) device shows up in the cache.
        url = _get_pod_server_url(
            pod_name, raise_on_error=False,
        ) if not force_refresh else None
        if force_refresh:
            _list_tailscale_devices(force=True)
            url = _get_pod_server_url(pod_name, raise_on_error=False)
            force_refresh = False
        if url:
            if url != last_logged_url:
                out(f"Resolved pod URL: {url}\n")
                last_logged_url = url
            last_url = url
            try:
                resp = _requests.get(f"{url}/system-info", timeout=5)
                if resp.ok:
                    resp.json()
                    out("Remote server is ready.\n")
                    # Best-effort: refresh ``gpu_type`` / ``datacenter`` /
                    # ``image`` on the persisted record now that the pod
                    # is allocated on a host. RunPod's create response
                    # often omits these, leaving the record permanently
                    # incomplete (visible as empty strings in GET /pods,
                    # especially after the pod transitions to EXITED).
                    rec = get_pod_record("runpod", pod_name)
                    if rec and rec.get("id"):
                        try:
                            backfill_pod_metadata(
                                _get_runpod_provider(),
                                pod_name,
                                rec["id"],
                            )
                        except Exception:
                            # Never fail readiness on metadata refresh.
                            pass
                    return url
            except Exception:
                pass
        remaining = int(deadline - time.monotonic())
        out(f"\rWaiting for pod '{pod_name}' server... ({remaining}s remaining)")
        time.sleep(poll_interval)
        # On each retry, force a Tailscale cache refresh so we pick up
        # newly-registered (possibly suffixed) hostnames.
        force_refresh = True
    raise RuntimeError(
        f"Pod '{pod_name}' server (last URL: {last_url or 'unresolved'}) "
        f"did not become ready within {timeout}s",
    )


_VALID_ON_OVERRUN = ("none", "stop", "terminate")
_VALID_PURPOSES = ("pr", "persistent", "test", "ci-runner")


def _safe_rel(base: Path, target: Path) -> str | None:
    """Return *target*'s POSIX-style relative path under *base*, or None
    if it doesn't lie under base. Used by the artifact endpoint to
    construct URL prefixes for nested fleet output dirs.
    """
    try:
        rel = target.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return str(rel).replace("\\", "/")


def _suite_report_from_dict(data: dict[str, Any]) -> Any:
    """Reconstruct a ``SuiteReport`` from its ``to_dict()`` payload.

    Used by the HTML report renderer to materialize a previously
    persisted ``report.json`` (or a fleet's nested per-target report).
    Comparison entries are reconstructed so the renderer's image-grid
    code can find ``diff_artifact`` paths.
    """
    from comfy_runner.testing.compare.registry import CompareResult
    from comfy_runner.testing.report import SuiteReport, WorkflowReport
    from comfy_runner.testing.runner import ComparisonEntry

    workflows = []
    for wf in data.get("workflows", []) or []:
        comparisons: list[ComparisonEntry] = []
        for comp in wf.get("comparisons", []) or []:
            res = comp.get("result", {}) or {}
            diff_artifact = res.get("diff_artifact")
            cr = CompareResult(
                method=res.get("method", "existence"),
                passed=bool(res.get("passed", False)),
                score=res.get("score"),
                threshold=res.get("threshold"),
                diff_artifact=Path(diff_artifact) if diff_artifact else None,
                details=res.get("details") or {},
            )
            comparisons.append(ComparisonEntry(
                baseline_file=comp.get("baseline_file", ""),
                test_file=comp.get("test_file", ""),
                result=cr,
            ))
        workflows.append(WorkflowReport(
            name=wf.get("name", ""),
            passed=bool(wf.get("passed", False)),
            error=wf.get("error"),
            execution_time=wf.get("execution_time"),
            output_count=int(wf.get("output_count", 0) or 0),
            has_baseline=bool(wf.get("has_baseline", False)),
            comparisons=comparisons,
        ))
    return SuiteReport(
        suite_name=data.get("suite_name", ""),
        timestamp=data.get("timestamp", ""),
        duration=float(data.get("duration", 0) or 0),
        total=int(data.get("total", 0) or 0),
        passed=int(data.get("passed", 0) or 0),
        failed=int(data.get("failed", 0) or 0),
        workflows=workflows,
        target_info=data.get("target_info") or {},
        timed_out=bool(data.get("timed_out", False)),
        aborted_reason=data.get("aborted_reason"),
    )


def _comfy_url_for_target(target_body: dict[str, Any]) -> str | None:
    """Resolve the ComfyUI HTTP URL for a target body (best-effort).

    Used by the watchdog's abort callback to send POST /interrupt to
    the live ComfyUI prompt. Returns ``None`` when the URL cannot be
    determined (e.g. ephemeral runpod targets that own their own
    interrupt path inside ``run_on_runpod``).
    """
    kind = target_body.get("kind")
    if kind == "remote":
        pn = target_body.get("pod_name")
        if pn:
            surl = _get_pod_server_url(pn, raise_on_error=False)
            if surl:
                return surl.rsplit(":", 1)[0] + ":8188"
            return None
        if target_body.get("server_url"):
            surl = target_body["server_url"]
            return surl.rsplit(":", 1)[0] + ":8188"
    elif kind == "local":
        url = target_body.get("url", "")
        if url:
            if "://" not in url:
                url = f"http://{url}"
            return url
    return None


def _interrupt_comfy(comfy_url: str) -> None:
    """Best-effort POST /interrupt to a ComfyUI URL — never raises."""
    try:
        from comfy_runner.testing.client import ComfyTestClient
        ComfyTestClient(comfy_url).interrupt()
    except Exception:
        pass


def _default_on_overrun_for_kind(kind: str) -> str:
    """Default ``on_overrun`` per target kind.

    - ``runpod`` (ephemeral) → ``terminate``
    - ``remote`` (PR-pod or persistent) → ``stop``
    - ``local`` → ``none``
    """
    if kind == "runpod":
        return "terminate"
    if kind == "remote":
        return "stop"
    return "none"


def _resolve_on_overrun(
    target_body: dict[str, Any],
    explicit: str | None,
) -> str:
    """Pick the effective on_overrun for one target."""
    if explicit:
        return explicit
    return _default_on_overrun_for_kind(target_body.get("kind", ""))


def _dispatch_on_overrun(
    target_body: dict[str, Any],
    on_overrun: str,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply the on_overrun pod action after a watchdog abort.

    Returns a dict describing what happened (``action``, ``skipped``,
    ``pod_name``, etc.) for inclusion in the test-run summary.
    """
    out = send_output or (lambda _: None)
    pod_name = target_body.get("pod_name")
    summary: dict[str, Any] = {
        "on_overrun": on_overrun,
        "pod_name": pod_name,
    }
    if on_overrun == "none" or not pod_name:
        summary["action"] = "none"
        return summary

    name_err = _validate_pod_name(pod_name)
    if name_err:
        summary["action"] = "skipped"
        summary["reason"] = name_err
        return summary

    from comfy_runner.hosted.config import (
        get_pod_record, remove_pod_record, update_pod_record,
    )

    try:
        provider = _get_runpod_provider()
    except Exception as e:
        summary["action"] = "skipped"
        summary["reason"] = f"provider unavailable: {e}"
        return summary

    rec = get_pod_record("runpod", pod_name)

    if on_overrun == "stop":
        if not rec:
            # Untracked pod → fall back to terminate.
            out(f"on_overrun=stop: '{pod_name}' is untracked, falling back to terminate\n")
            on_overrun = "terminate"
        else:
            lock = _get_pod_lock(pod_name)
            if not lock.acquire(timeout=10):
                out(f"on_overrun=stop: pod '{pod_name}' busy, skipping stop\n")
                summary["action"] = "skipped"
                summary["reason"] = "pod_busy"
                return summary
            try:
                pod_id = rec.get("id")
                if not pod_id:
                    summary["action"] = "skipped"
                    summary["reason"] = "no_pod_id"
                    return summary
                try:
                    pod = provider.get_pod(pod_id)
                except Exception as e:
                    summary["action"] = "skipped"
                    summary["reason"] = f"get_pod_failed: {e}"
                    return summary
                if not pod or pod.status != "RUNNING":
                    out(
                        f"on_overrun=stop: pod '{pod_name}' is "
                        f"{pod.status if pod else 'gone'}, skipping stop\n"
                    )
                    summary["action"] = "skipped"
                    summary["reason"] = (
                        f"not_running:{pod.status if pod else 'gone'}"
                    )
                    return summary
                try:
                    provider.stop_pod(pod_id)
                except Exception as e:
                    summary["action"] = "skipped"
                    summary["reason"] = f"stop_failed: {e}"
                    return summary

                def _mark_overrun(
                    r: dict[str, Any] | None,
                ) -> dict[str, Any] | None:
                    if r is None:
                        return None
                    r["status_hint"] = "stopped_overrun"
                    r["stopped_at"] = int(time.time())
                    return r

                update_pod_record("runpod", pod_name, _mark_overrun)
                out(f"on_overrun=stop: pod '{pod_name}' stopped\n")
                summary["action"] = "stopped"
                return summary
            finally:
                lock.release()

    if on_overrun == "terminate":
        if rec and rec.get("id"):
            # CRITICAL: only drop the registry record AFTER
            # ``terminate_pod`` returns without raising. The previous
            # version blindly removed the record even when terminate
            # failed, which produced registry-less RunPod pods that
            # were invisible to the orphan reaper and billed
            # indefinitely (real incident: 5 5090 pods leaked for
            # ~10h, ~$50). The orphan reconciler in
            # ``_orphan_pod_reconciliation`` is the safety net for any
            # future regression of this same shape, but the right fix
            # is to never remove the record on a failed terminate in
            # the first place.
            try:
                provider.terminate_pod(rec["id"])
            except Exception as e:
                out(
                    f"on_overrun=terminate: terminate_pod failed: {e} "
                    f"(record kept for the orphan reaper)\n"
                )
                summary["action"] = "skipped"
                summary["reason"] = f"terminate_failed: {e}"
                return summary
            try:
                remove_pod_record("runpod", pod_name)
            except Exception as e:
                out(
                    f"on_overrun=terminate: remove_pod_record failed: "
                    f"{e} (pod was terminated; stale record will be "
                    f"GC'd by reaper)\n"
                )
            out(f"on_overrun=terminate: pod '{pod_name}' terminated\n")
            summary["action"] = "terminated"
            return summary
        # No record (or no id) — nothing to do.
        out(
            f"on_overrun=terminate: pod '{pod_name}' has no tracked "
            f"record, skipping\n"
        )
        summary["action"] = "skipped"
        summary["reason"] = "no_record"
        return summary

    summary["action"] = "skipped"
    summary["reason"] = f"unknown_on_overrun:{on_overrun}"
    return summary


def _dispatch_auto_stop(
    target_body: dict[str, Any],
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Stop the pod referenced by *target_body* after a successful run.

    Used by ``/tests/run`` and ``/tests/fleet`` when the request includes
    ``auto_stop: true``. Unlike ``_dispatch_on_overrun``, this never
    falls back to terminate -- if the pod can't be stopped (untracked,
    busy, already stopped, etc.) the action is skipped and the pod is
    left running. The caller's idle reaper is the safety net.

    Returns a dict for inclusion in the test-run summary.
    """
    out = send_output or (lambda _: None)
    pod_name = target_body.get("pod_name")
    summary: dict[str, Any] = {"auto_stop": True, "pod_name": pod_name}

    if not pod_name:
        summary["action"] = "skipped"
        summary["reason"] = "no_pod_name"
        return summary

    name_err = _validate_pod_name(pod_name)
    if name_err:
        summary["action"] = "skipped"
        summary["reason"] = name_err
        return summary

    from comfy_runner.hosted.config import get_pod_record, update_pod_record

    try:
        provider = _get_runpod_provider()
    except Exception as e:
        summary["action"] = "skipped"
        summary["reason"] = f"provider unavailable: {e}"
        return summary

    rec = get_pod_record("runpod", pod_name)
    if not rec:
        out(f"auto_stop: '{pod_name}' is untracked, skipping\n")
        summary["action"] = "skipped"
        summary["reason"] = "untracked"
        return summary

    lock = _get_pod_lock(pod_name)
    if not lock.acquire(timeout=10):
        out(f"auto_stop: pod '{pod_name}' busy, skipping stop\n")
        summary["action"] = "skipped"
        summary["reason"] = "pod_busy"
        return summary
    try:
        pod_id = rec.get("id")
        if not pod_id:
            summary["action"] = "skipped"
            summary["reason"] = "no_pod_id"
            return summary
        try:
            pod = provider.get_pod(pod_id)
        except Exception as e:
            summary["action"] = "skipped"
            summary["reason"] = f"get_pod_failed: {e}"
            return summary
        if not pod or pod.status != "RUNNING":
            out(
                f"auto_stop: pod '{pod_name}' is "
                f"{pod.status if pod else 'gone'}, skipping stop\n"
            )
            summary["action"] = "skipped"
            summary["reason"] = (
                f"not_running:{pod.status if pod else 'gone'}"
            )
            return summary
        try:
            provider.stop_pod(pod_id)
        except Exception as e:
            summary["action"] = "skipped"
            summary["reason"] = f"stop_failed: {e}"
            return summary

        def _mark_auto_stopped(
            r: dict[str, Any] | None,
        ) -> dict[str, Any] | None:
            if r is None:
                return None
            r["status_hint"] = "stopped_post_test"
            r["stopped_at"] = int(time.time())
            return r

        update_pod_record("runpod", pod_name, _mark_auto_stopped)
        out(f"auto_stop: pod '{pod_name}' stopped\n")
        summary["action"] = "stopped"
        return summary
    finally:
        lock.release()


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
        if pod_name:
            _touch_pod_activity(pod_name)
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


def _normalize_repo_url(value: str) -> str:
    """Reduce a repo URL or ``owner/name`` slug to a canonical form.

    Strips scheme, ``github.com/`` prefix, ``.git`` suffix, leading/
    trailing slashes, and lower-cases. So ``https://github.com/Comfy-Org/ComfyUI.git``
    and ``Comfy-Org/ComfyUI`` both reduce to ``comfy-org/comfyui``.
    """
    if not value:
        return ""
    s = value.strip().lower()
    if "://" in s:
        s = s.split("://", 1)[1]
    if s.startswith("github.com/"):
        s = s[len("github.com/"):]
    if s.endswith(".git"):
        s = s[: -len(".git")]
    return s.strip("/")


def _pod_already_at_pr(
    runner: Any,
    install_name: str,
    pr: int,
    owner: str,
    repo: str,
    *,
    send_output: Callable[[str], None] | None = None,
) -> bool:
    """Best-effort check: is *install_name* on the pod already at this PR?

    Used to make ``POST /pods/{name}/review`` idempotent: re-reviewing a
    PR that's already deployed should be a no-op for the deploy step.
    Returns False on any error so the caller falls back to a real deploy
    rather than skipping incorrectly.
    """
    out = send_output or (lambda _t: None)
    try:
        info = runner._request("GET", f"/{install_name}/info")
    except Exception as e:
        out(f"  (skipping idempotency check: {e})\n")
        return False

    deployed_pr = info.get("deployed_pr")
    if deployed_pr is None:
        return False
    try:
        if int(deployed_pr) != int(pr):
            return False
    except (TypeError, ValueError):
        return False

    deployed_repo = info.get("deployed_repo") or ""
    requested_repo = f"{owner}/{repo}"
    if _normalize_repo_url(deployed_repo) != _normalize_repo_url(
        requested_repo
    ):
        return False
    return True


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
  .failed { color: #f44336; }
  a { color: #00d9ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .refresh { color: #555; font-size: 0.85rem; }
  .badge { display: inline-block; padding: 0.1rem 0.45rem; border-radius: 3px;
           font-size: 0.7rem; background: #2a2a4e; color: #94a3b8; margin-left: 0.4rem; }
  .badge.pr { background: #00d9ff22; color: #00d9ff; }
  .badge.persistent { background: #ff980022; color: #ff9800; }
  .badge.test, .badge.ci-runner { background: #f4433622; color: #f44336; }
</style>
</head>
<body>
<h1>comfy-runner Dashboard</h1>
<p class="refresh">Auto-refreshes every 15s.</p>

<h2>Comfy-Runners (Tailnet)</h2>
{% if tailnet.error %}
<p class="refresh">Tailnet discovery failed: {{ tailnet.error }}</p>
{% elif not tailnet.tailnet_configured %}
<p class="refresh">Tailscale API key / tailnet not configured —
discovery disabled.</p>
{% elif tailnet.runners %}
<p class="refresh">{{ tailnet.runners|length }} responder(s) of
{{ tailnet.online_count }} online tailnet device(s)
({{ tailnet.device_count }} total).</p>
<table>
<tr>
  <th>Name</th><th>Provider</th><th>Purpose</th>
  <th>GPU</th><th>RAM (GB)</th><th>$/hr</th>
  <th>PR #</th><th>Server URL</th>
</tr>
{% for r in tailnet.runners %}
<tr>
  <td>{{ r.hostname }}</td>
  <td class="{% if r.provider == 'runpod' %}running{% endif %}">{{ r.provider }}</td>
  <td>{% if r.purpose %}{{ r.purpose }}{% else %}-{% endif %}</td>
  <td>{% if r.gpu %}{{ r.gpu }}{% else %}-{% endif %}</td>
  <td>{% if r.ram_gb is not none %}{{ r.ram_gb }}{% else %}-{% endif %}</td>
  <td>{% if r.cost_per_hr is defined and r.cost_per_hr %}{{ "%.2f"|format(r.cost_per_hr) }}{% else %}-{% endif %}</td>
  <td>{% if r.pr_number %}<a href="https://github.com/comfyanonymous/ComfyUI/pull/{{ r.pr_number }}">#{{ r.pr_number }}</a>{% else %}-{% endif %}</td>
  <td><a href="{{ r.server_url }}">{{ r.server_url }}</a></td>
</tr>
{% endfor %}
</table>
{% else %}
<p>No comfy-runners detected on the tailnet
({{ tailnet.online_count }} online device(s) probed).</p>
{% endif %}

<h2>Pods (RunPod registry)</h2>
{% if pods %}
<table>
<tr><th>Name</th><th>Status</th><th>Purpose</th><th>GPU</th><th>$/hr</th><th>Server URL</th></tr>
{% for p in pods %}
<tr>
  <td>{{ p.name }}</td>
  <td class="{{ p.status|lower }}">{{ p.status }}</td>
  <td>{% if p.purpose %}<span class="badge {{ p.purpose }}">{{ p.purpose }}</span>{% else %}-{% endif %}</td>
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
<tr>
  <th>ID</th><th>Kind</th><th>Suite</th><th>Status</th>
  <th>Results</th><th>Report</th><th>Raw</th>
</tr>
{% for t in test_runs %}
<tr>
  <td>{{ t.id }}</td>
  <td>{{ t.kind }}</td>
  <td>{% set s = t.suite or "" %}{% if s|length > 60 %}{{ s[:60] }}…{% else %}{{ s }}{% endif %}</td>
  <td class="{{ t.status }}">{{ t.status }}</td>
  <td>
    {% if t.summary and t.summary.total_targets is defined %}
      {% set tp = t.summary.targets_passed|default(0) %}
      {% set tt = t.summary.total_targets|default(0) %}
      {% set tf = t.summary.targets_failed|default(0) %}
      <span class="{% if tf == 0 and tt > 0 %}running{% elif tf > 0 %}failed{% endif %}">{{ tp }}/{{ tt }}{% if tf > 0 %} ({{ tf }} failed){% endif %}</span>
    {% else %}
      -
    {% endif %}
  </td>
  <td><a href="/tests/{{ t.id }}/report?format=html">HTML</a> ·
      <a href="/tests/{{ t.id }}/report?format=markdown">MD</a></td>
  <td><a href="/tests/{{ t.id }}">JSON</a></td>
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

    # Restore persisted test-run history before serving any request.
    # Best-effort: a corrupt or missing file just leaves _test_runs empty.
    _load_persisted_test_runs()

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
            from comfy_runner.pip_utils import install_changed_requirements
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
                        build_kw = _extract_build_kwargs(body)
                        # comfyui_ref works in both prebuilt and build flows,
                        # so forward it whenever the user supplied it.
                        if "comfyui_ref" in body:
                            build_kw["comfyui_ref"] = body["comfyui_ref"]
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
                    force=bool(body.get("force", False)),
                    send_output=out,
                )

                # Install any deploy-tracked requirements files that changed.
                changed_files = result.get("changed_files", [])
                result["requirements_installed"] = install_changed_requirements(
                    install_path, changed_files, send_output=out
                )

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
    # PR review preparation
    # ==================================================================

    # ------------------------------------------------------------------
    # POST /reviews/local — fetch manifest + workflows + models for a PR
    # ------------------------------------------------------------------
    @app.route("/reviews/local", methods=["POST"])
    def route_reviews_local() -> Any:
        """Run prepare_local_review against a named installation.

        Body: ``{install, owner, repo, pr, github_token?, download_token?,
        extra_models?, extra_workflows?, allow_arbitrary_urls?,
        skip_provisioning?}``. Async — returns ``job_id`` whose
        ``result`` matches :func:`comfy_runner.review.prepare_local_review`.

        The deploy step is *not* run here — callers are expected to have
        already deployed the PR (e.g. via ``POST /<install>/deploy``) so
        this endpoint is composable with the existing deploy proxy.
        """
        from safe_file import is_safe_path_component

        body = request.get_json(silent=True) or {}

        install = body.get("install", "")
        if not isinstance(install, str) or not install or not is_safe_path_component(install):
            return _err("'install' is required and must be a safe identifier")

        owner = body.get("owner", "")
        repo = body.get("repo", "")
        if not isinstance(owner, str) or not owner.strip():
            return _err("'owner' is required")
        if not isinstance(repo, str) or not repo.strip():
            return _err("'repo' is required")

        pr = body.get("pr")
        if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
            return _err("'pr' must be a positive integer")

        extra_models_raw = body.get("extra_models") or []
        extra_workflows_raw = body.get("extra_workflows") or []
        if not isinstance(extra_models_raw, list):
            return _err("'extra_models' must be a list")
        if not isinstance(extra_workflows_raw, list) or not all(
            isinstance(w, str) for w in extra_workflows_raw
        ):
            return _err("'extra_workflows' must be a list of strings")

        from comfy_runner.manifest import ModelEntry
        try:
            extra_models = [ModelEntry.from_dict(m) for m in extra_models_raw]
        except (ValueError, AttributeError, TypeError) as e:
            return _err(f"invalid 'extra_models': {e}")

        github_token = body.get("github_token")
        if github_token is not None and not isinstance(github_token, str):
            return _err("'github_token' must be a string")
        download_token = body.get("download_token", "") or ""
        if not isinstance(download_token, str):
            return _err("'download_token' must be a string")

        allow_arbitrary_urls = bool(body.get("allow_arbitrary_urls", False))
        skip_provisioning = bool(body.get("skip_provisioning", False))

        record, err = _get_record(install)
        if not record:
            return _err(err, 404)
        install_path = record["path"]

        job_id = _jobs.create(
            label=f"review {owner}/{repo}#{pr} on {install}"
        )

        def _run() -> None:
            from comfy_runner.review import prepare_local_review

            out, lines = _make_collector(job_id)
            lock = _get_install_lock(install)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Installation '{install}' is busy", lines)
                return
            try:
                result = prepare_local_review(
                    install_path, owner, repo, int(pr),
                    github_token=github_token,
                    download_token=download_token,
                    extra_models=extra_models,
                    extra_workflows=list(extra_workflows_raw),
                    allow_arbitrary_urls=allow_arbitrary_urls,
                    skip_provisioning=skip_provisioning,
                    send_output=out,
                )
                _jobs.finish(job_id, result, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"ok": True, "job_id": job_id, "async": True})

    # ------------------------------------------------------------------
    # POST /reviews/cleanup — terminate ephemeral PR pods (purpose='pr')
    # ------------------------------------------------------------------
    @app.route("/reviews/cleanup", methods=["POST"])
    def route_reviews_cleanup() -> Any:
        """Terminate runpod pods that were created for a specific PR.

        Body: ``{pr: <int>, dry_run?: bool}``. Targets pods whose record
        has ``purpose == "pr"`` AND ``pr_number == <pr>``. Dry-run lists
        matches without acting. Persistent and test pods are never
        touched, even if their name happens to mention the PR.
        """
        from comfy_runner.hosted.config import (
            list_pod_records, remove_pod_record,
        )

        body = request.get_json(silent=True) or {}
        pr = body.get("pr")
        if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
            return _err("'pr' must be a positive integer")
        dry_run = bool(body.get("dry_run", False))

        try:
            provider = _get_runpod_provider()
        except RuntimeError as e:
            return _err(str(e))

        records = list_pod_records("runpod")
        candidates: list[tuple[str, dict]] = []
        for name, rec in records.items():
            if rec.get("purpose") != "pr":
                continue
            if rec.get("pr_number") != pr:
                continue
            candidates.append((name, rec))

        terminated: list[dict] = []
        skipped: list[dict] = []
        removed_records: list[str] = []
        if dry_run:
            for name, rec in candidates:
                skipped.append({
                    "name": name,
                    "id": rec.get("id", ""),
                    "reason": "dry-run",
                })
        else:
            for name, rec in candidates:
                pod_id = rec.get("id", "")
                try:
                    if pod_id:
                        provider.terminate_pod(pod_id)
                    terminated.append({"name": name, "id": pod_id})
                    try:
                        if remove_pod_record("runpod", name):
                            removed_records.append(name)
                    except Exception:
                        pass
                except Exception as e:
                    skipped.append({
                        "name": name,
                        "id": pod_id,
                        "error": str(e),
                    })

        return jsonify({
            "ok": True,
            "pr": pr,
            "dry_run": dry_run,
            "terminated": terminated,
            "skipped": skipped,
            "removed_records": removed_records,
            "total_found": len(candidates),
            "total_terminated": len(terminated),
        })

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

        # Optional ?purpose=<value> filter — case-sensitive string match
        # against the record field. Records without an explicit purpose
        # are treated as "persistent" for filtering only (matching the
        # POST /pods/create default).
        purpose_filter = request.args.get("purpose")

        try:
            live_pods = provider.list_pods()
            live_map = {p.name: p for p in live_pods}

            records = list_pod_records("runpod")
            result = []

            # Merge config records with live pod data
            seen_names = set()
            for name, rec in records.items():
                if purpose_filter is not None:
                    rec_purpose = rec.get("purpose", "persistent")
                    if rec_purpose != purpose_filter:
                        continue
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
                    # Prefer live values when present, else fall back to
                    # whatever was persisted at create / backfill time.
                    # This protects EXITED pods, whose ``list_pods``
                    # response usually drops the GPU / machine / image
                    # fields, from showing empty strings.
                    entry["gpu_type"] = live.gpu_type or entry["gpu_type"]
                    entry["datacenter"] = live.datacenter or entry["datacenter"]
                    entry["image"] = live.image or entry["image"]
                else:
                    entry["status"] = "UNKNOWN"
                # Activity / idle-reaper metadata
                if rec.get("purpose"):
                    entry["purpose"] = rec["purpose"]
                if rec.get("pr_number") is not None:
                    entry["pr_number"] = rec["pr_number"]
                if rec.get("last_active_at"):
                    entry["last_active_at"] = rec["last_active_at"]
                if rec.get("idle_timeout_s"):
                    entry["idle_timeout_s"] = rec["idle_timeout_s"]
                if rec.get("status_hint"):
                    entry["status_hint"] = rec["status_hint"]
                idle_remaining = _idle_seconds_remaining(rec)
                if idle_remaining is not None and entry["status"] == "RUNNING":
                    entry["idle_in_s"] = idle_remaining
                # Add URLs (handles Tailscale hostname drift)
                server_url = _get_pod_server_url(name, raise_on_error=False)
                if server_url:
                    entry["server_url"] = server_url
                    entry["comfy_url"] = server_url.rsplit(":", 1)[0] + ":8188"
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
        container_disk_gb = body.get("container_disk_gb")
        datacenter = body.get("datacenter")
        cloud_type = body.get("cloud_type")
        gpu_count = body.get("gpu_count", 1)
        env = body.get("env")
        wait_ready = body.get("wait_ready", True)
        # ``persistent`` by default; let the body override (e.g. ``test``,
        # though test pods normally come from the test runner path).
        purpose = body.get("purpose", "persistent")
        if purpose not in _VALID_PURPOSES:
            return _err(
                "'purpose' must be one of: " + ", ".join(_VALID_PURPOSES)
            )

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
                    try:
                        pod = provider.get_pod(rec["id"])
                    except Exception:
                        pod = None  # Pod gone from RunPod — stale record
                    if pod and pod.status not in ("TERMINATED", "EXITED"):
                        if pod.status != "RUNNING":
                            out(f"Pod '{name}' exists but is {pod.status}, starting...\n")
                            _start_pod_with_retry(
                                provider, rec["id"], send_output=out,
                            )
                        else:
                            out(f"Pod '{name}' already running.\n")
                        if wait_ready:
                            server_url = _wait_for_pod_server(name, send_output=out)
                        else:
                            server_url = _get_pod_server_url(
                                name, raise_on_error=False,
                            ) or ""
                        _touch_pod_activity(name)
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
                    container_disk_gb=container_disk_gb,
                    datacenter=datacenter,
                    cloud_type=cloud_type,
                    gpu_count=gpu_count,
                    env=env,
                )
                rec_payload: dict[str, Any] = {
                    "id": pod.id,
                    "gpu_type": pod.gpu_type,
                    "datacenter": pod.datacenter,
                    "image": pod.image,
                    "purpose": purpose,
                }
                # Stamp creation/activity time on purposes that are
                # subject to time-based reaping (test TTL, ci-runner /
                # pr idle timeout). Persistent pods don't need this.
                if purpose in ("test", "ci-runner", "pr"):
                    rec_payload["created_at"] = int(time.time())
                    rec_payload["last_active_at"] = int(time.time())
                set_pod_record("runpod", name, rec_payload)
                out(f"Pod created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)\n")

                if wait_ready:
                    server_url = _wait_for_pod_server(name, send_output=out)
                else:
                    server_url = _get_pod_server_url(
                        name, raise_on_error=False,
                    ) or ""

                _touch_pod_activity(name)
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
                    _start_pod_with_retry(
                        provider, pod_id, send_output=out,
                    )

                _touch_pod_activity(name)
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
                if body.get("force"):
                    deploy_body["force"] = True
                deploy_body.update(_extract_build_kwargs(body))
                if "comfyui_ref" in body:
                    deploy_body["comfyui_ref"] = body["comfyui_ref"]

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
    # POST /pods/<name>/review — auto-wake + deploy + prepare review (async)
    # ------------------------------------------------------------------
    @app.route("/pods/<name>/review", methods=["POST"])
    def route_pods_review(name: str) -> Any:
        """End-to-end review prep on an existing pod.

        Auto-wakes the pod if it is stopped, deploys the requested PR via
        the pod's sidecar ``/<install>/deploy`` route, and finally calls
        the sidecar's ``/reviews/local`` route. Returns a single station
        ``job_id``; its eventual ``result`` carries both
        ``deploy_result`` and ``review_result`` plus pod metadata.
        """
        from safe_file import is_safe_path_component

        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        from comfy_runner.hosted.config import get_pod_record

        rec = get_pod_record("runpod", name)
        if not rec:
            return _err(f"Pod '{name}' not found in config", 404)

        body = request.get_json(silent=True) or {}
        install_name = body.get("install", "main")
        if not isinstance(install_name, str) or not install_name or not is_safe_path_component(install_name):
            return _err("'install' must be a safe identifier")

        owner = body.get("owner", "")
        repo = body.get("repo", "")
        if not isinstance(owner, str) or not owner.strip():
            return _err("'owner' is required")
        if not isinstance(repo, str) or not repo.strip():
            return _err("'repo' is required")

        pr = body.get("pr")
        if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
            return _err("'pr' must be a positive integer")

        # Best-effort body validation; the sidecar re-validates everything
        # but we want to fail fast for obvious shape errors.
        extra_models = body.get("extra_models") or []
        extra_workflows = body.get("extra_workflows") or []
        if not isinstance(extra_models, list):
            return _err("'extra_models' must be a list")
        if not isinstance(extra_workflows, list) or not all(
            isinstance(w, str) for w in extra_workflows
        ):
            return _err("'extra_workflows' must be a list of strings")

        # When ``skip_deploy`` is set, the caller has already deployed
        # the PR (e.g. via POST /pods/launch-pr in the runpod-target
        # flow) and only wants the review-prep step to run.
        skip_deploy = bool(body.get("skip_deploy", False))

        # Default behavior is idempotent: if the pod's installation
        # already has the requested PR deployed, skip the deploy step.
        # ``force_deploy=true`` overrides and always deploys.
        force_deploy = bool(body.get("force_deploy", False))

        # Optional per-review idle timeout override. When set, the pod
        # record's ``idle_timeout_s`` is updated after the review-prep
        # call so the idle reaper uses the new value.
        idle_timeout_override = body.get("idle_timeout_s")
        if idle_timeout_override is not None:
            if (
                isinstance(idle_timeout_override, bool)
                or not isinstance(idle_timeout_override, int)
                or idle_timeout_override <= 0
            ):
                return _err(
                    "'idle_timeout_s' must be a positive integer"
                )

        # ── Purpose gating ───────────────────────────────────────────────
        # Reviews mutate install state on the pod. Match the pod's
        # ``purpose`` tag against what's safe:
        #   * "pr"          → designed for this; allow.
        #   * "persistent"  → general dev pod; allow with a one-line warning
        #                     so the user knows their dev install state is
        #                     about to be deployed-over.
        #   * "test"        → e2e test pods have curated install state we
        #                     shouldn't trample; refuse unless the caller
        #                     opts in with ``force_purpose: true``.
        #   * "ci-runner"   → long-lived test runner with cached models +
        #                     pinned ComfyUI commit; refuse like "test".
        force_purpose = bool(body.get("force_purpose", False))
        pod_purpose = rec.get("purpose", "persistent")
        if pod_purpose in ("test", "ci-runner") and not force_purpose:
            return _err(
                f"Pod '{name}' has purpose='{pod_purpose}' "
                f"({'e2e test pod' if pod_purpose == 'test' else 'CI test runner'}). "
                f"Reviews would clobber its curated install state. "
                f"Pass force_purpose=true to override.",
                409,
            )

        job_id = _jobs.create(
            label=f"pod review {owner}/{repo}#{pr} on {name}"
        )

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

                pod = provider.get_pod(pod_id)
                if not pod or pod.status in ("TERMINATED", "EXITED"):
                    _jobs.fail(
                        job_id,
                        f"Pod '{name}' is terminated or gone",
                        lines,
                    )
                    return
                if pod.status != "RUNNING":
                    out(f"Pod was {pod.status} — auto-waking...\n")
                    _start_pod_with_retry(
                        provider, pod_id, send_output=out,
                    )
                else:
                    out("Pod is RUNNING.\n")

                # Surface purpose so the operator knows what kind of pod
                # this review is about to mutate. ``pr`` pods are silent.
                if pod_purpose == "persistent":
                    out(
                        f"⚠ Pod '{name}' is purpose='persistent' "
                        f"(general-dev). Review will deploy over its "
                        f"current install state.\n"
                    )
                elif pod_purpose in ("test", "ci-runner") and force_purpose:
                    out(
                        f"⚠ Pod '{name}' is purpose='{pod_purpose}' but "
                        f"force_purpose=true — proceeding.\n"
                    )

                _touch_pod_activity(name)
                server_url = _get_pod_server_url(name)
                out(f"Connecting to {server_url}...\n")
                _wait_for_remote_server(server_url, send_output=out)

                runner = RemoteRunner(server_url)

                # ── 1) Deploy the PR via the sidecar ───────────────────
                # Skipped when ``skip_deploy=True`` — the runpod target
                # uses POST /pods/launch-pr first, which already deploys,
                # so this proxy just runs the review prep step.
                #
                # Otherwise — unless ``force_deploy=True`` — check whether
                # the pod's installation already has this PR deployed and
                # skip the deploy step. Reviewing the same PR twice in a
                # row should be a no-op for the deploy phase.
                if skip_deploy:
                    out(
                        "Skipping deploy step (skip_deploy=true).\n"
                    )
                    deploy_result = None
                elif not force_deploy and _pod_already_at_pr(
                    runner, install_name, pr, owner, repo, send_output=out,
                ):
                    out(
                        f"PR #{pr} ({owner}/{repo}) is already deployed "
                        f"on '{install_name}'. Skipping deploy "
                        f"(use force_deploy=true to override).\n"
                    )
                    deploy_result = {
                        "skipped": True,
                        "reason": "PR already deployed",
                    }
                else:
                    deploy_body: dict[str, Any] = {
                        "pr": int(pr),
                        "repo": f"https://github.com/{owner}/{repo}",
                        "start": True,
                    }
                    if body.get("title"):
                        deploy_body["title"] = body["title"]
                    if body.get("launch_args"):
                        deploy_body["launch_args"] = body["launch_args"]
                    if body.get("cuda_compat"):
                        deploy_body["cuda_compat"] = True

                    out(
                        f"Deploying PR #{pr} ({owner}/{repo}) "
                        f"to '{install_name}'...\n"
                    )
                    deploy_resp = runner._request(
                        "POST", f"/{install_name}/deploy", json=deploy_body,
                    )
                    deploy_remote_job = deploy_resp.get("job_id")
                    if not deploy_remote_job:
                        _jobs.fail(
                            job_id, "Deploy did not return a job_id", lines,
                        )
                        return
                    deploy_result = runner.poll_job(
                        deploy_remote_job, timeout=900, on_output=out,
                    )

                # ── 2) Prepare review via the sidecar ──────────────────
                review_body: dict[str, Any] = {
                    "install": install_name,
                    "owner": owner,
                    "repo": repo,
                    "pr": int(pr),
                }
                for k in (
                    "github_token", "download_token", "extra_models",
                    "extra_workflows", "allow_arbitrary_urls",
                    "skip_provisioning",
                ):
                    if k in body:
                        review_body[k] = body[k]

                out("\nPreparing review (manifest + workflows + models)...\n")
                review_resp = runner._request(
                    "POST", "/reviews/local", json=review_body,
                )
                review_remote_job = review_resp.get("job_id")
                if not review_remote_job:
                    _jobs.fail(
                        job_id, "Review prepare did not return a job_id",
                        lines,
                    )
                    return
                review_result = runner.poll_job(
                    review_remote_job, timeout=900, on_output=out,
                )

                # ── 3) Optional idle timeout override ───────────────────
                # Update the pod record so the idle reaper picks up the
                # new value on its next sweep. ``purpose='pr'`` only —
                # the reaper ignores other purposes anyway.
                idle_timeout_applied: int | None = None
                if idle_timeout_override is not None:
                    from comfy_runner.hosted.config import update_pod_record

                    def _set_idle(r: dict | None) -> dict | None:
                        if r is None:
                            return None
                        r["idle_timeout_s"] = int(idle_timeout_override)
                        return r
                    update_pod_record("runpod", name, _set_idle)
                    idle_timeout_applied = int(idle_timeout_override)
                    out(
                        f"Pod idle timeout updated to "
                        f"{idle_timeout_applied}s.\n"
                    )

                _jobs.finish(job_id, {
                    "pod_name": name,
                    "pod_purpose": pod_purpose,
                    "server_url": server_url,
                    "deploy_result": deploy_result,
                    "review_result": review_result,
                    "idle_timeout_s": idle_timeout_applied,
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
        try:
            start_retries = int(body.get("start_retries", DEFAULT_START_RETRIES))
        except (TypeError, ValueError):
            return _err("start_retries must be an integer")
        try:
            start_retry_delay_s = int(
                body.get("start_retry_delay_s", DEFAULT_START_RETRY_DELAY_S)
            )
        except (TypeError, ValueError):
            return _err("start_retry_delay_s must be an integer")

        job_id = _jobs.create(label=f"pod start {name}")

        def _run() -> None:
            out, lines = _make_collector(job_id)
            lock = _get_pod_lock(name)
            if not lock.acquire(timeout=5):
                _jobs.fail(job_id, f"Pod '{name}' is busy", lines)
                return
            try:
                out(f"Starting pod '{name}'...\n")
                # ``_ensure_pod_running`` wraps ``_start_pod_with_retry``
                # and additionally falls back to terminate+recreate (on
                # the same network volume) when the pod is stuck on a
                # capacity-exhausted host. The recreate-on-stuck branch
                # only triggers for ``ci-runner`` pods that have a
                # ``volume_id`` on their record; other pod types behave
                # exactly as before.
                server_url = _ensure_pod_running(
                    name,
                    send_output=out,
                    wait_ready=wait_ready,
                    start_retries=start_retries,
                    start_retry_delay_s=start_retry_delay_s,
                )

                # ``_ensure_pod_running`` may have updated the pod id
                # via ``_recreate_pod_for_capacity``; re-read the
                # record so the response reflects the live id.
                from comfy_runner.hosted.config import get_pod_record
                fresh_rec = get_pod_record("runpod", name) or rec
                _jobs.finish(job_id, {
                    "name": name,
                    "id": fresh_rec.get("id", rec["id"]),
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
            try:
                provider.terminate_pod(rec["id"])
            except Exception:
                pass  # Pod may already be gone on RunPod
            remove_pod_record("runpod", name)
            return jsonify({"ok": True, "name": name, "action": "terminated"})
        except Exception as e:
            return _err(str(e))
        finally:
            lock.release()
            _remove_pod_lock(name)

    # ------------------------------------------------------------------
    # POST /pods/<name>/touch — reset the idle timer
    # ------------------------------------------------------------------
    @app.route("/pods/<name>/touch", methods=["POST"])
    def route_pods_touch(name: str) -> Any:
        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        from comfy_runner.hosted.config import get_pod_record
        rec = get_pod_record("runpod", name)
        if not rec:
            return _err(f"Pod '{name}' not found in config", 404)

        _touch_pod_activity(name)
        rec = get_pod_record("runpod", name) or {}
        return jsonify({
            "ok": True,
            "name": name,
            "last_active_at": rec.get("last_active_at"),
            "idle_in_s": _idle_seconds_remaining(rec),
        })

    # ------------------------------------------------------------------
    # POST /pods/launch-pr — atomic create-or-wake + deploy a PR
    # ------------------------------------------------------------------
    @app.route("/pods/launch-pr", methods=["POST"])
    def route_pods_launch_pr() -> Any:
        from safe_file import is_safe_path_component

        body = request.get_json(silent=True) or {}
        pr = body.get("pr")
        if pr is None or not isinstance(pr, int):
            return _err("'pr' is required (integer PR number)")

        repo = body.get("repo") or ""
        # Derive a stable, sanitized pod name. ``pr-<repo>-<num>`` if a
        # repo is provided, else ``pr-<num>``. The repo segment maps
        # ``/`` to ``-`` (separating owner from name) and ``.`` to ``_``
        # so distinct repos like ``owner/my-repo`` and ``owner/my.repo``
        # produce distinct slugs (``owner-my-repo`` vs ``owner-my_repo``).
        repo_slug = ""
        if repo:
            # Strip protocol and trailing ``.git``, take last ``owner/name``.
            r = repo
            if "://" in r:
                r = r.split("://", 1)[1]
            if r.endswith(".git"):
                r = r[: -len(".git")]
            tail = r.rsplit("/", 2)[-2:]
            normalized = "/".join(tail).lower()
            # Map ``/`` → ``-`` and ``.`` → ``_``; everything else
            # outside ``[a-z0-9-_]`` collapses to ``-``.
            chars = []
            for c in normalized:
                if c == "/":
                    chars.append("-")
                elif c == ".":
                    chars.append("_")
                elif c.isalnum() or c in "-_":
                    chars.append(c)
                else:
                    chars.append("-")
            repo_slug = "".join(chars).strip("-_")
        name = f"pr-{repo_slug}-{pr}" if repo_slug else f"pr-{pr}"
        if not is_safe_path_component(name):
            return _err(f"Derived pod name '{name}' is not a safe identifier")

        gpu_type = body.get("gpu_type")
        image = body.get("image")
        volume_id = body.get("volume_id")
        volume_size_gb = body.get("volume_size_gb")
        container_disk_gb = body.get("container_disk_gb")
        datacenter = body.get("datacenter")
        cloud_type = body.get("cloud_type")
        gpu_count = body.get("gpu_count", 1)
        env = body.get("env")
        install_name = body.get("install", "main")
        raw_timeout = body.get("idle_timeout_s")
        if raw_timeout is None:
            idle_timeout_s = DEFAULT_IDLE_TIMEOUT_S
        else:
            try:
                idle_timeout_s = int(raw_timeout)
            except (TypeError, ValueError):
                return _err("'idle_timeout_s' must be an integer")
            if idle_timeout_s <= 0:
                return _err("'idle_timeout_s' must be > 0")

        job_id = _jobs.create(label=f"launch PR #{pr} ({name})")

        # Acquire the pod lock with enough headroom that an in-flight
        # reaper sweep (which makes a remote ``stop_pod`` call) does not
        # cause a spurious "Pod is busy" failure here.
        _LAUNCH_LOCK_TIMEOUT_S = 60

        def _run() -> None:
            from comfy_runner.hosted.config import (
                get_pod_record, set_pod_record, update_pod_record,
            )
            from comfy_runner.hosted.remote import RemoteRunner

            out, lines = _make_collector(job_id)
            lock = _get_pod_lock(name)
            if not lock.acquire(timeout=_LAUNCH_LOCK_TIMEOUT_S):
                _jobs.fail(job_id, f"Pod '{name}' is busy", lines)
                return
            try:
                provider = _get_runpod_provider()
                rec = get_pod_record("runpod", name)

                # ── Decide create / wake / reuse ────────────────────
                pod = None
                created_new = False
                if rec:
                    try:
                        pod = provider.get_pod(rec["id"])
                    except Exception:
                        pod = None
                    if not pod or pod.status == "TERMINATED":
                        out(f"Pod '{name}' record exists but pod is gone; recreating...\n")
                        rec = None

                if not rec:
                    out(f"Creating pod '{name}' for PR #{pr}...\n")
                    pod = provider.create_pod(
                        name=name,
                        gpu_type=gpu_type,
                        image=image,
                        volume_id=volume_id,
                        volume_size_gb=volume_size_gb,
                        container_disk_gb=container_disk_gb,
                        datacenter=datacenter,
                        cloud_type=cloud_type,
                        gpu_count=gpu_count,
                        env=env,
                    )
                    created_new = True
                    set_pod_record("runpod", name, {
                        "id": pod.id,
                        "gpu_type": pod.gpu_type,
                        "datacenter": pod.datacenter,
                        "image": pod.image,
                        "purpose": "pr",
                        "pr_number": pr,
                        "repo": repo,
                        "idle_timeout_s": idle_timeout_s,
                        # Stamp activity now so a subsequent failure
                        # before deploy still leaves the reaper able to
                        # see the pod as eligible to clean up.
                        "last_active_at": int(time.time()),
                    })
                    out(f"Pod created (id: {pod.id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)\n")
                else:
                    # Wake existing pod if needed; ensure metadata is set.
                    def _refresh_meta(r: dict[str, Any] | None) -> dict[str, Any] | None:
                        if r is None:
                            return None
                        r.setdefault("purpose", "pr")
                        r["pr_number"] = pr
                        if repo:
                            r["repo"] = repo
                        r["idle_timeout_s"] = idle_timeout_s
                        r["last_active_at"] = int(time.time())
                        r.pop("status_hint", None)
                        return r
                    update_pod_record("runpod", name, _refresh_meta)
                    if pod and pod.status != "RUNNING":
                        out(f"Pod '{name}' is {pod.status}, starting...\n")
                        _start_pod_with_retry(
                            provider, rec["id"], send_output=out,
                        )
                    else:
                        out(f"Pod '{name}' is already RUNNING.\n")

                # ── Wait for the comfy-runner server ────────────────
                server_url = _wait_for_pod_server(name, send_output=out)

                # ── Deploy the PR ──────────────────────────────────
                runner = RemoteRunner(server_url)
                deploy_body: dict[str, Any] = {
                    "pr": pr,
                    "start": True,
                }
                if repo:
                    deploy_body["repo"] = repo
                if body.get("title"):
                    deploy_body["title"] = body["title"]
                if body.get("launch_args"):
                    deploy_body["launch_args"] = body["launch_args"]

                out(f"Deploying PR #{pr} to '{install_name}'...\n")
                data = runner._request(
                    "POST", f"/{install_name}/deploy", json=deploy_body,
                )
                remote_job_id = data.get("job_id")
                deploy_result: Any
                if remote_job_id:
                    deploy_result = runner.poll_job(
                        remote_job_id, timeout=900, on_output=out,
                    )
                else:
                    deploy_result = data

                _touch_pod_activity(name)
                _jobs.finish(job_id, {
                    "name": name,
                    "pr": pr,
                    "created": created_new,
                    "server_url": server_url,
                    "comfy_url": server_url.rsplit(":", 1)[0] + ":8188",
                    "idle_timeout_s": idle_timeout_s,
                    "deploy_result": deploy_result,
                }, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({
            "ok": True, "job_id": job_id, "async": True,
            "name": name, "pr": pr,
        })

    # ------------------------------------------------------------------
    # POST /pods/create-ci-runner — provision a long-lived CI test pod
    #
    # Composed action: create pod (purpose='ci-runner') → wait → deploy
    # ``main`` (--latest) → preflight-download every model declared by
    # the named suites → stop the pod. Container disk persists models
    # across suspend/resume so subsequent CI invocations skip the
    # downloads. Idempotent: if the pod already exists with cached
    # models, the suites' preflight is a no-op and the pod is left
    # stopped.
    # ------------------------------------------------------------------
    @app.route("/pods/create-ci-runner", methods=["POST"])
    def route_pods_create_ci_runner() -> Any:
        body = request.get_json(silent=True) or {}
        name = body.get("name", "")
        name_err = _validate_pod_name(name)
        if name_err:
            return _err(name_err)

        suites = body.get("suites", [])
        if not isinstance(suites, list) or not suites:
            return _err("'suites' is required (non-empty list of suite names)")
        for s in suites:
            if not isinstance(s, str) or not s:
                return _err("each entry in 'suites' must be a non-empty string")

        # Resolve every suite up-front so we fail fast on missing ones.
        resolved_suites: list[tuple[str, Path]] = []
        for s in suites:
            sp, serr = _resolve_suite(s)
            if serr:
                return _err(f"suite '{s}': {serr}")
            resolved_suites.append((s, sp))

        gpu_type = body.get("gpu_type")
        image = body.get("image")
        datacenter = body.get("datacenter")
        cloud_type = body.get("cloud_type")
        container_disk_gb = body.get("container_disk_gb")
        gpu_count = body.get("gpu_count", 1)
        env = body.get("env")
        # Optional persistent network volume. If ``volume_size_gb`` is
        # provided without an explicit ``volume_id``, the handler will
        # find-or-create a volume named ``vol-<pod_name>`` in the
        # requested ``datacenter`` and attach it. Models on the volume
        # then survive a full pod termination (not just suspend/resume),
        # which avoids re-downloading multi-GB checkpoints when a host
        # has to be evicted due to capacity drought.
        volume_id = body.get("volume_id")
        raw_volume_size = body.get("volume_size_gb")
        volume_size_gb: int | None = None
        if raw_volume_size is not None:
            try:
                volume_size_gb = int(raw_volume_size)
            except (TypeError, ValueError):
                return _err("'volume_size_gb' must be an integer")
            if volume_size_gb <= 0:
                return _err("'volume_size_gb' must be > 0")
        if volume_size_gb is not None and not volume_id and not datacenter:
            return _err(
                "'datacenter' is required when 'volume_size_gb' is set "
                "without an explicit 'volume_id' "
                "(network volumes are tied to a specific datacenter)"
            )
        install_name = body.get("install", "main")
        install_err = _validate_pod_name(install_name)
        if install_err:
            return _err(f"invalid install name: {install_err}")
        raw_timeout = body.get("idle_timeout_s")
        if raw_timeout is None:
            idle_timeout_s = DEFAULT_IDLE_TIMEOUT_S
        else:
            try:
                idle_timeout_s = int(raw_timeout)
            except (TypeError, ValueError):
                return _err("'idle_timeout_s' must be an integer")
            if idle_timeout_s <= 0:
                return _err("'idle_timeout_s' must be > 0")

        raw_ready = body.get("pod_ready_timeout_s")
        if raw_ready is None:
            pod_ready_timeout_s = DEFAULT_POD_READY_TIMEOUT_S
        else:
            try:
                pod_ready_timeout_s = int(raw_ready)
            except (TypeError, ValueError):
                return _err("'pod_ready_timeout_s' must be an integer")
            if pod_ready_timeout_s <= 0:
                return _err("'pod_ready_timeout_s' must be > 0")

        # Whether to leave the pod stopped at the end (default True --
        # this is the whole point of pre-warmed CI runners). Tests can
        # disable it.
        leave_stopped = bool(body.get("leave_stopped", True))

        job_id = _jobs.create(label=f"create ci-runner {name}")

        def _run() -> None:
            from comfy_runner.hosted.config import (
                get_pod_record, set_pod_record, update_pod_record,
            )
            from comfy_runner.hosted.remote import RemoteRunner
            from comfy_runner.testing.preflight import ensure_suite_models
            from comfy_runner.testing.suite import load_suite

            out, lines = _make_collector(job_id)
            lock = _get_pod_lock(name)
            if not lock.acquire(timeout=60):
                _jobs.fail(job_id, f"Pod '{name}' is busy", lines)
                return
            try:
                provider = _get_runpod_provider()
                rec = get_pod_record("runpod", name)

                pod = None
                created_new = False
                if rec:
                    try:
                        pod = provider.get_pod(rec["id"])
                    except Exception:
                        pod = None
                    if not pod or pod.status == "TERMINATED":
                        out(
                            f"Pod '{name}' record exists but pod is gone; "
                            f"recreating...\n"
                        )
                        rec = None

                if not rec:
                    # Find-or-create the named persistent volume so the
                    # pod's /workspace survives a full pod termination
                    # (and not just suspend/resume).
                    resolved_volume_id = volume_id
                    # The datacenter the pod will actually be created
                    # in. If we reuse or attach to an existing volume,
                    # the volume's datacenter wins (RunPod requires
                    # dataCenterIds to match the volume's DC, otherwise
                    # the create call fails).
                    effective_datacenter = datacenter
                    if volume_size_gb is not None and not resolved_volume_id:
                        vol_name = f"vol-{name}"
                        existing_vol = None
                        try:
                            for v in provider.list_volumes():
                                if v.name == vol_name:
                                    existing_vol = v
                                    break
                        except Exception as e:
                            out(f"⚠ list_volumes failed ({e}); will create new volume.\n")
                        if existing_vol:
                            resolved_volume_id = existing_vol.id
                            if (
                                datacenter
                                and existing_vol.datacenter
                                and existing_vol.datacenter != datacenter
                            ):
                                out(
                                    f"⚠ Existing volume '{vol_name}' is in "
                                    f"datacenter '{existing_vol.datacenter}' "
                                    f"(requested '{datacenter}'); pod will be "
                                    f"pinned to the volume's datacenter.\n"
                                )
                            if existing_vol.datacenter:
                                effective_datacenter = existing_vol.datacenter
                            out(
                                f"Reusing existing volume '{vol_name}' "
                                f"(id: {resolved_volume_id}, "
                                f"{existing_vol.size_gb} GB, "
                                f"datacenter: {existing_vol.datacenter})\n"
                            )
                        else:
                            out(
                                f"Creating volume '{vol_name}' "
                                f"({volume_size_gb} GB, datacenter: "
                                f"{datacenter})...\n"
                            )
                            new_vol = provider.create_volume(
                                name=vol_name,
                                size_gb=volume_size_gb,
                                datacenter=datacenter,
                            )
                            resolved_volume_id = new_vol.id
                            if new_vol.datacenter:
                                effective_datacenter = new_vol.datacenter
                            out(
                                f"Volume created (id: {resolved_volume_id})\n"
                            )

                    out(f"Creating ci-runner pod '{name}'...\n")
                    pod = provider.create_pod(
                        name=name,
                        gpu_type=gpu_type,
                        image=image,
                        volume_id=resolved_volume_id,
                        container_disk_gb=container_disk_gb,
                        datacenter=effective_datacenter,
                        cloud_type=cloud_type,
                        gpu_count=gpu_count,
                        env=env,
                    )
                    created_new = True
                    new_record: dict[str, Any] = {
                        "id": pod.id,
                        "gpu_type": pod.gpu_type,
                        "datacenter": pod.datacenter or effective_datacenter,
                        "image": pod.image,
                        "purpose": "ci-runner",
                        "suites": list(suites),
                        "container_disk_gb": container_disk_gb,
                        "idle_timeout_s": idle_timeout_s,
                        "created_at": int(time.time()),
                        "last_active_at": int(time.time()),
                    }
                    if resolved_volume_id:
                        new_record["volume_id"] = resolved_volume_id
                    if volume_size_gb is not None:
                        new_record["volume_size_gb"] = volume_size_gb
                    set_pod_record("runpod", name, new_record)
                    out(
                        f"Pod created (id: {pod.id}, {pod.gpu_type}, "
                        f"${pod.cost_per_hr}/hr)\n"
                    )
                else:
                    # Existing ci-runner -- refresh metadata and wake.
                    def _refresh(r: dict[str, Any] | None) -> dict[str, Any] | None:
                        if r is None:
                            return None
                        r.setdefault("purpose", "ci-runner")
                        # Merge the suite list rather than replace, so
                        # repeat invocations with overlapping suites
                        # accumulate the union.
                        existing = list(r.get("suites") or [])
                        for s in suites:
                            if s not in existing:
                                existing.append(s)
                        r["suites"] = existing
                        if container_disk_gb is not None:
                            r["container_disk_gb"] = container_disk_gb
                        r["idle_timeout_s"] = idle_timeout_s
                        r["last_active_at"] = int(time.time())
                        r.pop("status_hint", None)
                        return r
                    update_pod_record("runpod", name, _refresh)
                    if pod and pod.status != "RUNNING":
                        out(f"Pod '{name}' is {pod.status}, starting...\n")
                        _start_pod_with_retry(
                            provider, rec["id"], send_output=out,
                        )
                    else:
                        out(f"Pod '{name}' is already RUNNING.\n")

                # Wait for comfy-runner server.
                server_url = _wait_for_pod_server(
                    name, timeout=pod_ready_timeout_s, send_output=out,
                )

                runner = RemoteRunner(server_url)

                # Deploy main at --latest.
                out(f"Deploying '{install_name}' at --latest...\n")
                deploy_data = runner._request(
                    "POST", f"/{install_name}/deploy",
                    json={"latest": True, "start": True},
                )
                remote_job_id = deploy_data.get("job_id")
                if remote_job_id:
                    runner.poll_job(
                        remote_job_id, timeout=1800, on_output=out,
                    )
                _touch_pod_activity(name)

                # Resolve ComfyUI URL for /object_info refresh.
                comfy_url = server_url.rsplit(":", 1)[0] + ":8188"

                # Preflight every suite's models.
                preflight_summary: list[dict[str, Any]] = []
                for sname, spath in resolved_suites:
                    out(f"Preflight: suite '{sname}'\n")
                    suite = load_suite(str(spath))
                    summary = ensure_suite_models(
                        runner, install_name, suite,
                        send_output=out, comfy_url=comfy_url,
                    )
                    preflight_summary.append({
                        "suite": sname,
                        **summary,
                    })

                # Stop the pod (optional).
                stop_action: dict[str, Any] = {"stopped": False}
                if leave_stopped:
                    out(f"Stopping pod '{name}' (cached, ready for CI)...\n")
                    try:
                        provider.stop_pod(rec["id"] if rec else pod.id)
                        stop_action = {"stopped": True}

                        def _mark_stopped(
                            r: dict[str, Any] | None,
                        ) -> dict[str, Any] | None:
                            if r is None:
                                return None
                            r["status_hint"] = "stopped_post_ci_warmup"
                            r["stopped_at"] = int(time.time())
                            return r

                        update_pod_record("runpod", name, _mark_stopped)
                    except Exception as e:
                        out(f"Warning: failed to stop pod: {e}\n")
                        stop_action = {"stopped": False, "error": str(e)}

                _jobs.finish(job_id, {
                    "name": name,
                    "created": created_new,
                    "server_url": server_url,
                    "suites": list(suites),
                    "idle_timeout_s": idle_timeout_s,
                    "preflight": preflight_summary,
                    "stopped": stop_action.get("stopped", False),
                }, lines)
            except Exception as e:
                _jobs.fail(job_id, str(e), lines)
            finally:
                lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({
            "ok": True, "job_id": job_id, "async": True, "name": name,
        })

    # ------------------------------------------------------------------
    # POST /pods/cleanup — terminate orphaned test pods
    # ------------------------------------------------------------------
    @app.route("/pods/cleanup", methods=["POST"])
    def route_pods_cleanup() -> Any:
        from comfy_runner.hosted.config import remove_pod_record

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
            removed_records: list[str] = []
            for pod in candidates:
                if dry_run:
                    skipped.append({"name": pod.name, "id": pod.id, "status": pod.status})
                else:
                    try:
                        provider.terminate_pod(pod.id)
                        terminated.append({"name": pod.name, "id": pod.id})
                        # Mirror DELETE /pods/<name>: drop the registry
                        # entry so cleanup leaves no dangling record.
                        try:
                            if remove_pod_record("runpod", pod.name):
                                removed_records.append(pod.name)
                        except Exception:
                            pass
                    except Exception as e:
                        skipped.append({"name": pod.name, "id": pod.id, "error": str(e)})

            return jsonify({
                "ok": True,
                "prefix": prefix,
                "dry_run": dry_run,
                "terminated": terminated,
                "skipped": skipped,
                "removed_records": removed_records,
                "total_found": len(candidates),
                "total_terminated": len(terminated),
            })
        except Exception as e:
            return _err(str(e))

    # ------------------------------------------------------------------
    # POST /pods/self-update — fan out /self-update across the tailnet
    # ------------------------------------------------------------------
    @app.route("/pods/self-update", methods=["POST"])
    def route_pods_self_update() -> Any:
        """Fan out ``POST /self-update`` to one, several, or all
        comfy-runners auto-discovered on the tailnet.

        Body (JSON, all fields optional)::

            {
                "names": ["pod-a", "pod-b"],   // null/empty = all online
                "force":  false                 // forwarded to each pod
            }

        Names may match either a runner's hostname (e.g.
        ``comfy-pr-1234``) or its pod_name (e.g. ``pr-1234``). Names
        that don't resolve to a reachable runner are returned with
        ``ok: false`` and an explanatory error in the per-target
        result; they do not abort the rest of the sweep.

        The central station's own hostname is always excluded from a
        ``--all`` sweep (rebooting the station mid-fan-out would
        orphan in-flight responses for the other pods); use the
        station's own ``POST /self-update`` for that.
        """
        import socket
        from safe_file import is_safe_path_component
        from comfy_runner.hosted.fanout import fanout_self_update
        from comfy_runner.hosted.tailnet import discover_comfy_runners

        body = request.get_json(silent=True) or {}
        names_raw = body.get("names") or []
        force = bool(body.get("force", False))

        if not isinstance(names_raw, list):
            return _err("'names' must be a list of strings")
        names: list[str] = []
        for n in names_raw:
            if not isinstance(n, str) or not is_safe_path_component(n):
                return _err(f"Invalid pod name: {n!r}")
            names.append(n)

        try:
            disc = discover_comfy_runners(force_refresh=True)
        except Exception as e:
            return _err(f"Tailnet discovery failed: {e}", 503)

        runners: list[dict[str, Any]] = list(disc.get("runners", []) or [])

        # Always exclude the station from a fleet sweep.
        self_host = socket.gethostname() or ""
        self_short = self_host.split(".", 1)[0].lower() if self_host else ""
        if self_short:
            runners = [
                r for r in runners
                if (r.get("hostname") or "").lower() != self_short
            ]

        targets: list[dict[str, Any]]
        unresolved: list[str] = []
        if names:
            by_hostname = {r["hostname"]: r for r in runners}
            by_pod_name = {r["pod_name"]: r for r in runners if r.get("pod_name")}
            targets = []
            for n in names:
                hit = by_hostname.get(n) or by_pod_name.get(n)
                if hit is None:
                    unresolved.append(n)
                else:
                    targets.append(hit)
        else:
            targets = runners

        results = fanout_self_update(targets, force=force)
        # Surface unresolved names as failed results so the caller sees
        # one row per requested name.
        for n in unresolved:
            results.append({
                "name": n, "host": "", "ok": False, "status": 404,
                "updated": False, "message": "",
                "error": "no reachable comfy-runner found with that hostname or pod_name",
            })

        ok_count = sum(1 for r in results if r["ok"])
        updated_count = sum(1 for r in results if r["ok"] and r.get("updated"))
        failed_count = len(results) - ok_count

        return jsonify({
            "ok": failed_count == 0,
            "results": results,
            "total": len(results),
            "ok_count": ok_count,
            "updated_count": updated_count,
            "failed_count": failed_count,
            "skipped_self": self_short or None,
        })

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
        body_max_runtime = body.get("max_runtime_s")
        if body_max_runtime is not None:
            if not isinstance(body_max_runtime, int) or body_max_runtime <= 0:
                return _err("'max_runtime_s' must be a positive integer")
        body_on_overrun = body.get("on_overrun")
        if body_on_overrun is not None and body_on_overrun not in _VALID_ON_OVERRUN:
            return _err(
                "'on_overrun' must be one of: "
                f"{', '.join(_VALID_ON_OVERRUN)}"
            )
        body_auto_stop = bool(body.get("auto_stop", False))

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

                # Resolve effective budget: body override beats suite.
                budget = body_max_runtime
                if budget is None and isinstance(s.max_runtime_s, int):
                    budget = s.max_runtime_s

                from comfy_runner.testing.client import watchdog as _watchdog

                def _on_abort() -> None:
                    out(
                        f"Watchdog: max_runtime_s={budget} exceeded "
                        f"— aborting test run\n",
                    )
                    comfy_url = _comfy_url_for_target(target_body)
                    if comfy_url:
                        _interrupt_comfy(comfy_url)

                with _watchdog(budget, on_abort=_on_abort) as cancelled:
                    result = target.run(
                        suite=s,
                        output_dir=out_dir,
                        timeout=timeout,
                        send_output=out,
                        cancelled=cancelled,
                    )

                summary = result.to_dict()
                aborted = bool(
                    cancelled.is_set()
                    or (
                        result.report is not None
                        and getattr(result.report, "timed_out", False)
                    )
                )
                summary["timed_out"] = aborted
                if aborted:
                    summary["aborted_reason"] = "overrun"

                # Dispatch on_overrun pod action if applicable.
                if aborted:
                    on_overrun = _resolve_on_overrun(
                        target_body, body_on_overrun,
                    )
                    pod_action = _dispatch_on_overrun(
                        target_body, on_overrun, send_output=out,
                    )
                    summary["on_overrun_action"] = pod_action
                elif body_auto_stop:
                    # Park the pod after a successful (or just-failed
                    # but not timed-out) run.
                    summary["auto_stop_action"] = _dispatch_auto_stop(
                        target_body, send_output=out,
                    )

                run_status = "timed_out" if aborted else "done"
                _finish_test_run(job_id, {
                    "output_dir": str(out_dir),
                    "summary": summary,
                    "timed_out": aborted,
                }, status=run_status)
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
        body_max_runtime = body.get("max_runtime_s")
        if body_max_runtime is not None:
            if not isinstance(body_max_runtime, int) or body_max_runtime <= 0:
                return _err("'max_runtime_s' must be a positive integer")
        body_on_overrun = body.get("on_overrun")
        if body_on_overrun is not None and body_on_overrun not in _VALID_ON_OVERRUN:
            return _err(
                "'on_overrun' must be one of: "
                f"{', '.join(_VALID_ON_OVERRUN)}"
            )
        body_auto_stop = bool(body.get("auto_stop", False))

        target_names = [t.name for t in targets]
        job_id = _jobs.create(label=f"fleet test ({len(targets)} targets)")
        _register_test_run(job_id, {
            "kind": "fleet",
            "suite": suite_name,
            "targets": target_bodies,
        })

        def _run() -> None:
            from comfy_runner.testing.suite import load_suite
            out, lines = _make_collector(job_id)
            try:
                run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                out_dir = suite_path / "runs" / f"fleet-{run_id}"

                # Resolve effective budget: body override beats suite.
                s = load_suite(str(suite_path))
                budget = body_max_runtime
                if budget is None and isinstance(s.max_runtime_s, int):
                    budget = s.max_runtime_s

                from comfy_runner.testing.client import watchdog as _watchdog

                def _on_abort() -> None:
                    out(
                        f"Watchdog: max_runtime_s={budget} exceeded "
                        f"— aborting fleet run\n",
                    )

                with _watchdog(budget, on_abort=_on_abort) as cancelled:
                    fleet_result = run_fleet(
                        targets=targets,
                        suite_path=str(suite_path),
                        output_dir=out_dir,
                        timeout=timeout,
                        max_workers=max_workers,
                        send_output=out,
                        formats=formats,
                        cancelled=cancelled,
                    )

                summary = fleet_result.to_dict()
                aborted = cancelled.is_set()
                summary["timed_out"] = aborted
                if aborted:
                    summary["aborted_reason"] = "overrun"
                    actions = []
                    for tb in target_bodies:
                        on_overrun = _resolve_on_overrun(tb, body_on_overrun)
                        actions.append(_dispatch_on_overrun(
                            tb, on_overrun, send_output=out,
                        ))
                    summary["on_overrun_actions"] = actions
                elif body_auto_stop:
                    summary["auto_stop_actions"] = [
                        _dispatch_auto_stop(tb, send_output=out)
                        for tb in target_bodies
                    ]

                run_status = "timed_out" if aborted else "done"
                _finish_test_run(job_id, {
                    "output_dir": str(out_dir),
                    "summary": summary,
                    "timed_out": aborted,
                }, status=run_status)
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
    # POST /tests/fleet-ci — run multiple suites across pre-warmed CI
    # runner pods. Each pod is started, runs all its assigned suites
    # sequentially via the remote target path (with preflight), then
    # is auto-stopped (default) to preserve cached models. Pods run in
    # parallel. The aggregate result is recorded as a single fleet
    # test-run so a single browsable HTML report covers everything.
    # ------------------------------------------------------------------
    @app.route("/tests/fleet-ci", methods=["POST"])
    def route_tests_fleet_ci() -> Any:
        from comfy_runner.hosted.config import get_pod_record

        body = request.get_json(silent=True) or {}
        assignments = body.get("assignments", [])
        if not isinstance(assignments, list) or not assignments:
            return _err(
                "'assignments' is required (list of "
                "{pod_name, suites} objects)"
            )

        # Validate every assignment up-front and resolve each suite
        # path. We refuse to start a fleet-ci run if any pod or suite
        # is bad, since the cost of a partial provision is high.
        validated: list[dict[str, Any]] = []
        for idx, a in enumerate(assignments):
            if not isinstance(a, dict):
                return _err(
                    f"assignments[{idx}] must be an object"
                )
            pod_name = a.get("pod_name", "")
            name_err = _validate_pod_name(pod_name)
            if name_err:
                return _err(f"assignments[{idx}].pod_name: {name_err}")
            rec = get_pod_record("runpod", pod_name)
            if not rec:
                return _err(
                    f"assignments[{idx}]: pod '{pod_name}' not "
                    f"found in registry",
                )
            suites_in = a.get("suites", [])
            if not isinstance(suites_in, list) or not suites_in:
                return _err(
                    f"assignments[{idx}].suites: must be a non-empty list"
                )
            resolved: list[tuple[str, Path]] = []
            for s in suites_in:
                sp, serr = _resolve_suite(s)
                if serr:
                    return _err(
                        f"assignments[{idx}] suite '{s}': {serr}",
                    )
                resolved.append((s, sp))
            validated.append({
                "pod_name": pod_name,
                "suites": resolved,
            })

        timeout = body.get("timeout", 600)
        body_max_runtime = body.get("max_runtime_s")
        if body_max_runtime is not None:
            if not isinstance(body_max_runtime, int) or body_max_runtime <= 0:
                return _err("'max_runtime_s' must be a positive integer")
        body_on_overrun = body.get("on_overrun")
        if (
            body_on_overrun is not None
            and body_on_overrun not in _VALID_ON_OVERRUN
        ):
            return _err(
                "'on_overrun' must be one of: "
                f"{', '.join(_VALID_ON_OVERRUN)}"
            )
        auto_stop = bool(body.get("auto_stop", True))

        raw_ready = body.get("pod_ready_timeout_s")
        if raw_ready is None:
            pod_ready_timeout_s = DEFAULT_POD_READY_TIMEOUT_S
        else:
            try:
                pod_ready_timeout_s = int(raw_ready)
            except (TypeError, ValueError):
                return _err("'pod_ready_timeout_s' must be an integer")
            if pod_ready_timeout_s <= 0:
                return _err("'pod_ready_timeout_s' must be > 0")

        job_id = _jobs.create(
            label=f"fleet-ci ({len(validated)} pods)"
        )
        # Treat fleet-ci as a fleet kind so the report endpoint
        # correctly aggregates per-target outputs into a single HTML
        # page.
        _register_test_run(job_id, {
            "kind": "fleet",
            "suite": ",".join(
                sorted({s for a in validated for s, _ in a["suites"]}),
            ),
            "targets": [
                {"kind": "remote", "pod_name": a["pod_name"]}
                for a in validated
            ],
        })

        def _run() -> None:
            from comfy_runner.testing.fleet import (
                FleetResult, RemoteTarget, TargetResult,
                _make_safe_dirname, _write_fleet_summary,
            )
            from comfy_runner.testing.suite import load_suite

            out, lines = _make_collector(job_id)

            run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            out_dir = _get_suites_dir().parent / "fleet-ci-runs" / f"fleet-{run_id}"
            out_dir.mkdir(parents=True, exist_ok=True)
            t0 = time.monotonic()

            target_results: list[TargetResult] = []
            results_lock = threading.Lock()

            def _run_pod(idx: int, assignment: dict[str, Any]) -> None:
                pod_name = assignment["pod_name"]
                suites = assignment["suites"]  # list of (name, path)
                pod_dir = out_dir / f"{idx}-{_make_safe_dirname(pod_name)}"
                pod_dir.mkdir(parents=True, exist_ok=True)

                def pod_out(text: str) -> None:
                    with results_lock:
                        out(f"[{pod_name}] {text}")

                try:
                    pod_out("Waking pod...\n")
                    server_url = _ensure_pod_running(
                        pod_name, send_output=pod_out, wait_ready=True,
                        pod_ready_timeout_s=pod_ready_timeout_s,
                    )

                    for sname, spath in suites:
                        suite_dir = pod_dir / sname
                        suite_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            target = RemoteTarget(
                                server_url=server_url,
                                install_name="main",
                                label=f"{pod_name}/{sname}",
                            )
                            suite = load_suite(str(spath))
                            tres = target.run(
                                suite=suite,
                                output_dir=suite_dir,
                                timeout=timeout,
                                send_output=pod_out,
                            )
                            with results_lock:
                                target_results.append(tres)
                        except Exception as exc:
                            pod_out(f"Suite '{sname}' errored: {exc}\n")
                            with results_lock:
                                target_results.append(TargetResult(
                                    target_name=f"{pod_name}/{sname}",
                                    target_kind="remote",
                                    output_dir=suite_dir,
                                    error=str(exc),
                                ))

                    # Auto-stop the pod (default).
                    if auto_stop:
                        _dispatch_auto_stop(
                            {"kind": "remote", "pod_name": pod_name},
                            send_output=pod_out,
                        )
                except Exception as exc:
                    pod_out(f"Pod errored: {exc}\n")
                    with results_lock:
                        target_results.append(TargetResult(
                            target_name=pod_name,
                            target_kind="remote",
                            output_dir=pod_dir,
                            error=str(exc),
                        ))

            try:
                # Fan out one thread per pod. Pods are independent.
                threads = [
                    threading.Thread(
                        target=_run_pod, args=(i, a), daemon=True,
                        name=f"fleet-ci-{a['pod_name']}",
                    )
                    for i, a in enumerate(validated)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                fleet_result = FleetResult(
                    suite_name=f"fleet-ci ({len(validated)} pods)",
                    results=target_results,
                    total_duration=time.monotonic() - t0,
                )
                _write_fleet_summary(fleet_result, out_dir, formats="json")

                summary = fleet_result.to_dict()
                summary["assignments"] = [
                    {"pod_name": a["pod_name"],
                     "suites": [s for s, _ in a["suites"]]}
                    for a in validated
                ]
                _finish_test_run(job_id, {
                    "output_dir": str(out_dir),
                    "summary": summary,
                    "test_id": job_id,
                }, status="done")
                _jobs.finish(job_id, {
                    "run_id": run_id,
                    "test_id": job_id,
                    "output_dir": str(out_dir),
                    **summary,
                }, lines)
            except Exception as e:
                _finish_test_run(job_id, {"error": str(e)}, status="error")
                _jobs.fail(job_id, str(e), lines)

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({
            "ok": True, "job_id": job_id, "async": True,
            "pods": [a["pod_name"] for a in validated],
        })

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
    # POST /tests/<test_id>/promote-baselines —
    #     mirror the CLI ``test baseline --approve-all`` flow across
    #     every suite in a (single or fleet) test run.
    #
    # This is the missing link between "ran tests, got outputs" and
    # "now those outputs are the expected results so the next run can
    # be graded against them via SSIM / video_frame_ssim / etc".
    # ------------------------------------------------------------------
    @app.route("/tests/<test_id>/promote-baselines", methods=["POST"])
    def route_tests_promote_baselines(test_id: str) -> Any:
        import shutil

        with _test_runs_lock:
            run = _test_runs.get(test_id)
        if not run:
            return _err(f"Test run '{test_id}' not found", 404)

        # We only promote from successful runs by default.  Pass
        # ``allow_failed=true`` to ignore the per-target pass flag (rare
        # — usually only useful when promoting one workflow that
        # happened to pass inside a partially-failed run).
        body = request.get_json(silent=True) or {}
        allow_failed = bool(body.get("allow_failed", False))

        # Collect (suite_name, output_dir) tuples for each suite that
        # produced outputs. For fleet runs we walk the per-target
        # results recorded in ``summary``; for single runs we use the
        # run's top-level output_dir + suite name.
        targets: list[tuple[str, Path]] = []
        is_fleet = run.get("kind") == "fleet"
        if is_fleet:
            results = (run.get("summary") or {}).get("results") or []
            for r in results:
                if not allow_failed and not r.get("passed", False):
                    continue
                out_sub = r.get("output_dir")
                target_name = r.get("target_name") or ""
                # ``target_name`` is ``pod/suite`` for fleet-CI; fall back
                # to the leaf directory name if the format ever changes.
                if "/" in target_name:
                    suite_name = target_name.rsplit("/", 1)[-1]
                elif out_sub:
                    suite_name = Path(out_sub).name
                else:
                    continue
                if not out_sub:
                    continue
                targets.append((suite_name, Path(out_sub)))
        else:
            out_dir = run.get("output_dir")
            if not out_dir:
                return _err("Test run has no output directory yet")
            if not allow_failed and run.get("status") != "done":
                return _err(
                    "Refusing to promote: run is not done. "
                    "Pass allow_failed=true to override.",
                )
            suite_name = run.get("suite") or ""
            if not suite_name:
                return _err("Could not determine suite name for this run")
            targets.append((suite_name, Path(out_dir)))

        if not targets:
            return _err(
                "No promotable targets found (all targets failed?). "
                "Pass allow_failed=true to promote anyway.",
            )

        # Promote each suite. Per-suite errors are accumulated into the
        # response instead of aborting so a single broken suite cannot
        # prevent the rest from getting baselines.
        from comfy_runner.testing.suite import load_suite

        per_suite: list[dict[str, Any]] = []
        total_approved = 0
        any_error = False
        for suite_name, output_dir in targets:
            entry: dict[str, Any] = {"suite": suite_name, "output_dir": str(output_dir)}
            try:
                suite_path, err = _resolve_suite(suite_name)
                if err:
                    entry["error"] = err
                    any_error = True
                    per_suite.append(entry)
                    continue
                suite = load_suite(suite_path)
            except Exception as e:  # noqa: BLE001
                entry["error"] = f"Could not load suite: {e}"
                any_error = True
                per_suite.append(entry)
                continue

            if not output_dir.is_dir():
                entry["error"] = f"Output dir missing: {output_dir}"
                any_error = True
                per_suite.append(entry)
                continue

            baselines_dir = suite.baselines_dir
            approved: list[str] = []
            skipped: list[str] = []
            collisions: list[str] = []

            # Mirror the CLI's ``_test_baseline`` logic exactly so the
            # dashboard route + the CLI converge on identical layouts.
            for wf in suite.workflows:
                wf_name = wf.stem
                wf_run_dir = output_dir / wf_name
                if not wf_run_dir.is_dir():
                    skipped.append(wf_name)
                    continue
                output_files = [
                    f for f in wf_run_dir.rglob("*") if f.is_file()
                ]
                if not output_files:
                    skipped.append(wf_name)
                    continue
                bl_dir = baselines_dir / wf_name
                bl_dir.mkdir(parents=True, exist_ok=True)
                seen: set[str] = set()
                for src in output_files:
                    if src.name in seen:
                        collisions.append(f"{wf_name}/{src.name}")
                    seen.add(src.name)
                    try:
                        shutil.copy2(str(src), str(bl_dir / src.name))
                    except OSError as e:
                        entry.setdefault("copy_errors", []).append(
                            f"{wf_name}/{src.name}: {e}"
                        )
                        any_error = True
                approved.append(wf_name)
                total_approved += 1

            entry["approved"] = approved
            entry["skipped"] = skipped
            if collisions:
                entry["collisions"] = collisions
            per_suite.append(entry)

        return jsonify({
            "ok": True,
            "test_id": test_id,
            "suites": per_suite,
            "total_workflows_approved": total_approved,
            "errors": any_error,
        })

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

        if fmt == "html":
            # Render on-the-fly with an artifact URL prefix so the
            # ``<img>`` srcs resolve through GET /tests/{id}/artifact/.
            from flask import Response

            from comfy_runner.testing.report import (
                SuiteReport, WorkflowReport, render_html,
            )
            from comfy_runner.testing.compare.registry import CompareResult
            from comfy_runner.testing.runner import ComparisonEntry

            if is_fleet:
                # Fleet HTML: aggregate per-target reports, each
                # rooted at its own subdir under the run.
                fleet_file = out_path / "fleet-report.json"
                if not fleet_file.is_file():
                    return _err("fleet-report.json not found")
                import json as _json

                fleet_data = _json.loads(fleet_file.read_text(encoding="utf-8"))
                parts: list[str] = []
                for tgt in fleet_data.get("results", []):
                    rdata = tgt.get("report")
                    out_sub = tgt.get("output_dir")
                    if not rdata or not out_sub:
                        continue
                    rel_subdir = _safe_rel(out_path, Path(out_sub))
                    if rel_subdir is None:
                        continue
                    artifact_prefix = (
                        f"/tests/{test_id}/artifact/{rel_subdir}"
                    )
                    sub_report = _suite_report_from_dict(rdata)
                    parts.append(
                        f"<h2 style='margin-top:2rem'>"
                        f"Target: {sub_report.target_info.get('name', '?')} "
                        f"({tgt.get('target_kind', '?')})</h2>"
                    )
                    parts.append(render_html(
                        sub_report,
                        artifact_url_prefix=artifact_prefix,
                    ))
                html_doc = (
                    "<!DOCTYPE html><html><head>"
                    "<meta charset='utf-8'>"
                    f"<title>Fleet test report {test_id}</title>"
                    "</head><body>"
                    f"<h1>Fleet test {test_id}</h1>"
                    + "\n".join(parts)
                    + "</body></html>"
                )
                return Response(html_doc, mimetype="text/html")

            single_file = out_path / "report.json"
            if not single_file.is_file():
                return _err("report.json not found")
            import json as _json

            data = _json.loads(single_file.read_text(encoding="utf-8"))
            report_obj = _suite_report_from_dict(data)
            artifact_prefix = f"/tests/{test_id}/artifact"
            html = render_html(report_obj, artifact_url_prefix=artifact_prefix)
            return Response(html, mimetype="text/html")

        ext_map = {
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

    # ------------------------------------------------------------------
    # GET /tests/<test_id>/artifact/<path:rel_path> — serve an artifact
    # file (output image, baseline, *_ssim_diff.png, etc.) referenced
    # by the HTML report. Path is resolved relative to the run's
    # output_dir and confined within it (path-traversal-safe).
    # ------------------------------------------------------------------
    @app.route(
        "/tests/<test_id>/artifact/<path:rel_path>",
        methods=["GET"],
    )
    def route_tests_artifact(test_id: str, rel_path: str) -> Any:
        from flask import send_file

        with _test_runs_lock:
            run = _test_runs.get(test_id)
        if not run:
            return _err(f"Test run '{test_id}' not found", 404)

        output_dir = run.get("output_dir")
        if not output_dir:
            return _err("Test run has no output directory yet (still running?)")

        base = Path(output_dir).resolve()
        # Reject any segment that escapes the base directory. We do
        # this both by rejecting absolute paths and ``..`` components
        # before joining, and by re-checking the resolved path is
        # contained in *base* afterwards.
        from safe_file import is_safe_path_component

        if rel_path.startswith("/") or rel_path.startswith("\\"):
            return _err("Invalid artifact path", 400)
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
        if not parts:
            return _err("Invalid artifact path", 400)
        for p in parts:
            if not is_safe_path_component(p):
                return _err(f"Invalid artifact path component: {p!r}", 400)

        target = (base.joinpath(*parts)).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return _err("Path escapes test-run output dir", 400)

        if not target.is_file():
            return _err(f"Artifact not found: {rel_path}", 404)

        return send_file(str(target))

    # ==================================================================
    # Tailnet — auto-discovery of comfy-runners on the configured tailnet
    # ==================================================================

    # ------------------------------------------------------------------
    # GET /tailnet/runners — list comfy-runners reachable on the tailnet
    # ------------------------------------------------------------------
    @app.route("/tailnet/runners", methods=["GET"])
    def route_tailnet_runners() -> Any:
        """Auto-discover comfy-runner instances on the configured tailnet.

        Probes ``/system-info`` on every online Tailscale device in
        parallel; only responders are returned. Each runner is enriched
        with hardware info (from system-info) and pod metadata
        (provider/purpose/pr_number) when joinable against a configured
        runpod pod record.

        Query parameters:
        * ``refresh=1`` — bypass the 30 s device-list cache.
        """
        from comfy_runner.hosted.tailnet import discover_comfy_runners

        force = request.args.get("refresh", "0") in ("1", "true", "yes")
        try:
            result = discover_comfy_runners(force_refresh=force)
        except Exception as e:
            return _err(f"Tailnet discovery failed: {e}", 503)
        return jsonify(result)

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
                server_url = _get_pod_server_url(pname, raise_on_error=False) or ""
                pods_data.append({
                    "name": pname,
                    "id": rec.get("id", ""),
                    "status": live.status if live else "UNKNOWN",
                    "gpu_type": (live.gpu_type if live else "") or rec.get("gpu_type", ""),
                    "cost_per_hr": live.cost_per_hr if live else 0,
                    "server_url": server_url,
                    # Surface the pod's purpose tag so the dashboard can
                    # badge it and the JS layer can warn before launching
                    # a review against a ``test`` / ``ci-runner`` pod.
                    "purpose": rec.get("purpose", "persistent"),
                })
        except Exception:
            pods_data = []

        # Tailnet discovery — best-effort; render an empty section
        # rather than failing the whole page if discovery raises. The
        # ``error`` field distinguishes a real failure (Tailscale API
        # down, transport error) from the "not configured" path so the
        # template can show an actionable message instead of silently
        # claiming discovery is disabled.
        tailnet_data: dict[str, Any] = {
            "ok": True, "runners": [], "tailnet_configured": False,
            "device_count": 0, "online_count": 0, "error": None,
        }
        try:
            from comfy_runner.hosted.tailnet import discover_comfy_runners
            tailnet_data = discover_comfy_runners()
            tailnet_data.setdefault("error", None)
        except Exception as e:
            log.warning("Dashboard tailnet discovery failed: %s", e)
            tailnet_data = {
                "ok": False, "runners": [], "tailnet_configured": True,
                "device_count": 0, "online_count": 0, "error": str(e),
            }

        test_runs = _list_test_runs(limit=20)
        active_jobs = _jobs.list_active()

        html = _DASHBOARD_HTML
        return render_template_string(
            html,
            pods=pods_data,
            tailnet=tailnet_data,
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
    _start_idle_reaper_once()
    log.info("Starting comfy-runner control server on %s:%d", host, port)
    print(f"comfy-runner server listening on http://{host}:{port}")
    serve(app, host=host, port=port, threads=8)
