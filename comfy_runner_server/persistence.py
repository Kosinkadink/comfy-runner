"""On-disk persistence for the central server's mutable in-memory state.

Currently scoped to ``_test_runs`` (the dict tracking test run metadata for
the dashboard and ``GET /tests`` endpoints). Without persistence the
dashboard "Recent Test Runs" section is empty on every server restart —
self-update, reboot, or crash all wipe history — which makes the
dashboard useless as an audit trail.

Design choices:

- **Storage location** mirrors the rest of the codebase: a JSON file under
  ``~/.comfy-runner/`` (overridable via ``COMFY_RUNNER_HOME``). The file
  uses the same ``atomic_write`` / ``atomic_read`` helpers as
  ``comfy_runner.config`` so a crash mid-write cannot corrupt history.

- **Versioned envelope** (``{"version": 1, "runs": {...}}``) leaves room
  for a schema migration without breaking old files.

- **Debounced writes** — fleet-CI runs produce many in-place ``status``
  updates as suites complete. Saving on every mutation would burn disk
  I/O; instead a single background timer coalesces writes (default 2 s
  trailing debounce). The debounce is implemented with a single shared
  ``threading.Timer`` that resets on each call.

- **Best-effort**. Persistence is a UX nicety, not the source of truth
  for running orchestration. Save failures are logged but never raised;
  load failures fall back to an empty registry so the server still boots.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("comfy_runner_server.persistence")


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

def _default_state_dir() -> Path:
    """Return the directory used for persisted server state."""
    base = Path(os.environ.get("COMFY_RUNNER_HOME", Path.home() / ".comfy-runner"))
    return base / "server_state"


def test_runs_path() -> Path:
    """Return the on-disk path for the persisted test-runs registry."""
    return _default_state_dir() / "test_runs.json"


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1


def load_test_runs() -> dict[str, dict[str, Any]]:
    """Load the persisted ``_test_runs`` registry from disk.

    Returns an empty dict if no file exists, the file is unreadable, or
    the schema version is unrecognized.  The server falls back to
    starting with no history rather than failing to boot.
    """
    # Imported lazily to avoid a hard dependency for tests that don't
    # exercise persistence (and to mirror how the rest of the server
    # uses ``safe_file``).
    from safe_file import atomic_read

    path = test_runs_path()
    raw = atomic_read(path)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Could not parse %s: %s — starting with empty registry", path, e)
        return {}
    if not isinstance(data, dict):
        log.warning("Unexpected shape in %s (not a dict) — discarding", path)
        return {}
    version = data.get("version")
    if version != _SCHEMA_VERSION:
        log.warning(
            "Unknown test_runs schema version %r in %s — discarding",
            version, path,
        )
        return {}
    runs = data.get("runs")
    if not isinstance(runs, dict):
        return {}
    # Defensive copy + drop any non-dict entries that snuck in via a bug.
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in runs.items():
        if isinstance(value, dict):
            cleaned[str(key)] = value
    return cleaned


def save_test_runs(runs: dict[str, dict[str, Any]]) -> None:
    """Persist ``runs`` to disk atomically.  Best-effort — never raises."""
    from safe_file import atomic_write

    path = test_runs_path()
    envelope = {"version": _SCHEMA_VERSION, "runs": runs}
    try:
        atomic_write(
            path,
            json.dumps(envelope, indent=2, default=str) + "\n",
            backup=True,
        )
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log.warning("Failed to persist test_runs to %s: %s", path, e)


# ---------------------------------------------------------------------------
# Debounced saver
#
# The caller passes a snapshot-producer (typically a lambda that copies
# ``_test_runs`` under its lock). The debouncer schedules a single
# background write at ``delay`` seconds in the future; subsequent calls
# within the window reset the timer so we coalesce bursts (e.g. a
# fleet-CI run with 31 suites each emitting a status update).
# ---------------------------------------------------------------------------

class DebouncedSaver:
    """Coalesce save_test_runs() calls into a single trailing write.

    Usage::

        saver = DebouncedSaver(snapshot=lambda: dict(_test_runs), delay=2.0)
        saver.schedule()      # call after every mutation
        saver.flush()         # synchronous write, e.g. on shutdown
    """

    def __init__(
        self,
        snapshot: "callable[[], dict[str, dict[str, Any]]]",
        delay: float = 2.0,
        save_fn: "callable[[dict[str, dict[str, Any]]], None]" = save_test_runs,
    ) -> None:
        self._snapshot = snapshot
        self._delay = delay
        self._save_fn = save_fn
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def schedule(self) -> None:
        """Schedule (or reschedule) a trailing-edge save."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """Cancel any pending timer and write synchronously."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._fire()

    def _fire(self) -> None:
        try:
            snap = self._snapshot()
        except Exception as e:  # noqa: BLE001
            log.warning("DebouncedSaver snapshot failed: %s", e)
            return
        self._save_fn(snap)
