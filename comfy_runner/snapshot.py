"""Snapshot capture, restore, diff, export/import — mirrors snapshots.ts.

Desktop 2.0-compatible snapshot format. See snapshots-spec.md for full schema.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SNAPSHOTS_DIR = Path(".launcher") / "snapshots"
AUTO_SNAPSHOT_LIMIT = 200
VALID_TRIGGERS = {"boot", "restart", "manual", "pre-update", "post-update", "post-restore"}
VALID_PIP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Per-install mutex — mirrors snapshots.ts withLock
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_lock(key: str) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshots_dir(install_path: str | Path) -> Path:
    return Path(install_path) / SNAPSHOTS_DIR


def _format_timestamp(dt: datetime) -> str:
    return (
        f"{dt.year:04d}{dt.month:02d}{dt.day:02d}_"
        f"{dt.hour:02d}{dt.minute:02d}{dt.second:02d}_"
        f"{dt.microsecond // 1000:03d}"
    )


def _iso_now() -> str:
    """Return an ISO 8601 timestamp matching Desktop 2.0's toISOString().

    JS toISOString() produces '2026-03-17T06:57:21.729Z'.
    Python isoformat() produces '2026-03-17T06:57:21.729217+00:00'.
    We truncate to milliseconds and use 'Z' suffix for compatibility.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp, handling both 'Z' and '+00:00' suffixes.

    Python < 3.11 doesn't support 'Z' in fromisoformat().
    Desktop 2.0 uses JS toISOString() which always produces 'Z'.
    """
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _node_to_camel(node: dict[str, Any]) -> dict[str, Any]:
    """Convert a scanned node dict from Python snake_case to Desktop 2.0 camelCase.

    The Python scanner (nodes.py) emits 'dir_name', but the Desktop 2.0
    snapshot schema uses 'dirName'. This conversion ensures saved snapshots
    are cross-compatible.
    """
    out = dict(node)
    if "dir_name" in out:
        out["dirName"] = out.pop("dir_name")
    return out


def _resolve_snapshot_path(install_path: str | Path, filename: str) -> Path | None:
    """Validate and resolve a snapshot filename. Returns None if invalid."""
    if not filename or filename != os.path.basename(filename):
        return None
    if not filename.endswith(".json"):
        return None
    snap_dir = _snapshots_dir(install_path).resolve()
    resolved = (snap_dir / filename).resolve()
    if not str(resolved).startswith(str(snap_dir) + os.sep):
        return None
    return resolved


def _read_manifest(install_path: str | Path) -> dict[str, str]:
    """Read manifest.json from install root."""
    try:
        data = json.loads((Path(install_path) / "manifest.json").read_text("utf-8"))
        return {
            "comfyui_ref": data.get("comfyui_ref", "unknown"),
            "version": data.get("version", ""),
            "id": data.get("id", ""),
        }
    except (OSError, json.JSONDecodeError):
        return {"comfyui_ref": "unknown", "version": "", "id": ""}


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture_state(
    install_path: str | Path,
) -> dict[str, Any]:
    """Capture the current environment state (ComfyUI version, nodes, pip).

    Returns a dict with comfyui, customNodes, pipPackages fields
    (matching Snapshot schema minus createdAt/trigger/label/version).
    """
    from .git_utils import read_git_head
    from .nodes import scan_custom_nodes
    from .pip_utils import pip_freeze

    install_path = Path(install_path)
    comfyui_dir = install_path / "ComfyUI"
    manifest = _read_manifest(install_path)
    commit = read_git_head(str(comfyui_dir))
    custom_nodes = scan_custom_nodes(install_path)

    pip_packages: dict[str, str] = {}
    try:
        pip_packages = pip_freeze(install_path)
    except Exception as e:
        # Non-fatal — pip freeze can fail if env is incomplete
        import sys
        print(f"Snapshot: pip freeze failed: {e}", file=sys.stderr)

    return {
        "comfyui": {
            "ref": manifest["comfyui_ref"],
            "commit": commit,
            "releaseTag": manifest["version"],
            "variant": manifest["id"],
        },
        "customNodes": [_node_to_camel(n) for n in custom_nodes],
        "pipPackages": pip_packages,
    }


# ---------------------------------------------------------------------------
# External / Manual Install Capture
# ---------------------------------------------------------------------------

_VENV_CANDIDATES = (".venv", "venv", ".env", "env")


def _find_venv_python(comfyui_dir: Path) -> Path | None:
    """Find a Python binary in common venv locations inside a ComfyUI dir.

    Mirrors Desktop 2.0 git.ts findVenv + getVenvPython — checks .venv,
    venv, .env, env for a pyvenv.cfg marker.
    """
    for name in _VENV_CANDIDATES:
        venv_dir = comfyui_dir / name
        if not (venv_dir / "pyvenv.cfg").exists():
            continue
        if sys.platform == "win32":
            py = venv_dir / "Scripts" / "python.exe"
            if py.exists():
                return py
            py = venv_dir / "python.exe"
            if py.exists():
                return py
        else:
            py = venv_dir / "bin" / "python3"
            if py.exists():
                return py
            py = venv_dir / "bin" / "python"
            if py.exists():
                return py
    return None


def pip_freeze_direct(python_path: str | Path) -> dict[str, str]:
    """Run ``python -m pip freeze --local`` against an arbitrary Python binary.

    Unlike pip_utils.pip_freeze this does NOT require uv or a comfy-runner
    managed installation — it works with any venv.  Mirrors Desktop 2.0
    desktopDetect.ts pipFreezeDirect.
    """
    result = subprocess.run(
        [str(python_path), "-m", "pip", "freeze", "--local"],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[:500]
        raise RuntimeError(f"pip freeze failed: {detail}")

    packages: dict[str, str] = {}
    for line in result.stdout.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        # Editable installs: "-e git+https://...@commit#egg=name"
        if trimmed.startswith("-e "):
            m = re.search(r"#egg=(.+)", trimmed)
            if m:
                packages[m.group(1)] = trimmed
            continue
        # PEP 508 direct references: "package @ git+https://..."
        at_match = re.match(r"^([A-Za-z0-9_.-]+)\s*@\s*(.+)$", trimmed)
        if at_match:
            packages[at_match.group(1)] = at_match.group(2).strip()
            continue
        # Standard: "package==version"
        eq_idx = trimmed.find("==")
        if eq_idx > 0:
            packages[trimmed[:eq_idx]] = trimmed[eq_idx + 2:]

    return packages


def capture_external_state(
    comfyui_dir: str | Path,
    venv_path: str | Path | None = None,
) -> dict[str, Any]:
    """Capture snapshot state from a manual/portable ComfyUI install.

    Unlike capture_state (which expects a comfy-runner managed install_path
    with ``install_path/ComfyUI/``), this takes the ComfyUI directory itself
    as the root.  Mirrors Desktop 2.0 localMigration.ts captureLocalSnapshot.

    Parameters
    ----------
    comfyui_dir:
        Path to the ComfyUI git clone (the directory containing ``main.py``
        and ``custom_nodes/``).
    venv_path:
        Optional explicit path to a venv directory.  If omitted, common
        locations (``.venv``, ``venv``, etc.) inside *comfyui_dir* are probed.
    """
    from .git_utils import read_git_head
    from .nodes import scan_custom_nodes_dir

    comfyui_dir = Path(comfyui_dir)
    commit = read_git_head(str(comfyui_dir))
    custom_nodes = scan_custom_nodes_dir(comfyui_dir / "custom_nodes")

    # Resolve Python binary
    python: Path | None = None
    if venv_path is not None:
        venv = Path(venv_path)
        if sys.platform == "win32":
            for candidate in (venv / "Scripts" / "python.exe", venv / "python.exe"):
                if candidate.exists():
                    python = candidate
                    break
        else:
            for candidate in (venv / "bin" / "python3", venv / "bin" / "python"):
                if candidate.exists():
                    python = candidate
                    break
        if python is None:
            raise RuntimeError(
                f"Python not found in specified venv: {venv_path}"
            )
    else:
        python = _find_venv_python(comfyui_dir)

    pip_packages: dict[str, str] = {}
    if python is not None:
        try:
            pip_packages = pip_freeze_direct(python)
        except Exception as e:
            print(f"Snapshot: pip freeze failed: {e}", file=sys.stderr)

    return {
        "comfyui": {
            "ref": "manual",
            "commit": commit,
            "releaseTag": "",
            "variant": "",
        },
        "customNodes": [_node_to_camel(n) for n in custom_nodes],
        "pipPackages": pip_packages,
    }


def _states_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Check if two snapshot states are identical."""
    from .nodes import node_key

    # ComfyUI version/commit
    ac, bc = a["comfyui"], b["comfyui"]
    if ac.get("ref") != bc.get("ref") or ac.get("commit") != bc.get("commit"):
        return False

    # Custom nodes
    a_nodes = a.get("customNodes", [])
    b_nodes = b.get("customNodes", [])
    if len(a_nodes) != len(b_nodes):
        return False
    a_map = {node_key(n): n for n in a_nodes}
    for bn in b_nodes:
        an = a_map.get(node_key(bn))
        if an is None:
            return False
        if (an.get("type") != bn.get("type") or an.get("version") != bn.get("version")
                or an.get("commit") != bn.get("commit") or an.get("enabled") != bn.get("enabled")):
            return False

    # Pip packages
    a_pips = a.get("pipPackages", {})
    b_pips = b.get("pipPackages", {})
    if len(a_pips) != len(b_pips):
        return False
    for key, val in a_pips.items():
        if b_pips.get(key) != val:
            return False

    return True


def _write_snapshot(install_path: str | Path, data: dict[str, Any]) -> str:
    """Write a snapshot to disk. Returns the filename."""
    now = datetime.now(timezone.utc)
    snapshot = {
        "version": 1,
        "createdAt": _iso_now(),
        "trigger": data["trigger"],
        "label": data.get("label"),
        "comfyui": data["comfyui"],
        "customNodes": data["customNodes"],
        "pipPackages": data["pipPackages"],
    }
    # Optional fields
    for key in ("skipPipSync", "pythonVersion", "updateChannel"):
        if key in data and data[key] is not None:
            snapshot[key] = data[key]

    snap_dir = _snapshots_dir(install_path)
    snap_dir.mkdir(parents=True, exist_ok=True)
    suffix = os.urandom(3).hex()
    filename = f"{_format_timestamp(now)}-{data['trigger']}-{suffix}.json"
    file_path = snap_dir / filename
    tmp_path = file_path.with_suffix(f".{suffix}.tmp")
    tmp_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    tmp_path.rename(file_path)
    return filename


def capture_snapshot_if_changed(
    install_path: str | Path,
    trigger: str = "boot",
    last_snapshot: str | None = None,
) -> dict[str, Any]:
    """Capture and save a snapshot if state changed from the last one.

    Returns {"saved": bool, "filename": str | None, "deduplicated": str | None}.
    """
    lock = _get_lock(str(install_path))
    with lock:
        current = capture_state(install_path)

        # Skip save if state unchanged (all auto triggers, not manual)
        if last_snapshot and trigger != "manual":
            try:
                last = load_snapshot(install_path, last_snapshot)
                if _states_match(last, current):
                    return {"saved": False, "filename": None, "deduplicated": None}
            except Exception:
                pass  # Last snapshot unreadable — save a new one

        filename = _write_snapshot(install_path, {**current, "trigger": trigger, "label": None})

        # Deduplicate restart snapshots
        deduplicated = None
        if trigger == "restart":
            try:
                deduplicated = _deduplicate_restart_snapshot(install_path, filename)
            except Exception:
                pass

        # Prune old auto snapshots
        try:
            prune_auto_snapshots(install_path, AUTO_SNAPSHOT_LIMIT)
        except Exception:
            pass

        return {"saved": True, "filename": filename, "deduplicated": deduplicated}


def save_snapshot(
    install_path: str | Path,
    trigger: str = "manual",
    label: str | None = None,
) -> str:
    """Save a snapshot unconditionally. Returns filename."""
    lock = _get_lock(str(install_path))
    with lock:
        current = capture_state(install_path)
        return _write_snapshot(install_path, {**current, "trigger": trigger, "label": label})


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_snapshots(install_path: str | Path) -> list[dict[str, Any]]:
    """List all snapshots, newest first. Each entry has 'filename' and 'snapshot'."""
    snap_dir = _snapshots_dir(install_path)
    if not snap_dir.exists():
        return []
    entries = []
    for f in sorted(snap_dir.iterdir()):
        if not f.suffix == ".json":
            continue
        try:
            data = json.loads(f.read_text("utf-8"))
            entries.append({"filename": f.name, "snapshot": data})
        except (OSError, json.JSONDecodeError):
            continue
    # Sort newest first
    entries.sort(key=lambda e: e["snapshot"].get("createdAt", ""), reverse=True)
    return entries


def load_snapshot(install_path: str | Path, filename: str) -> dict[str, Any]:
    """Load a single snapshot by filename."""
    file_path = _resolve_snapshot_path(install_path, filename)
    if file_path is None:
        raise ValueError(f"Invalid snapshot filename: {filename}")
    return json.loads(file_path.read_text("utf-8"))


def delete_snapshot(install_path: str | Path, filename: str) -> None:
    """Delete a single snapshot by filename."""
    file_path = _resolve_snapshot_path(install_path, filename)
    if file_path is None:
        raise ValueError(f"Invalid snapshot filename: {filename}")
    file_path.unlink()


def get_snapshot_count(install_path: str | Path) -> int:
    """Return total number of snapshots on disk."""
    return len(list_snapshots(install_path))


# ---------------------------------------------------------------------------
# Pruning & Deduplication
# ---------------------------------------------------------------------------

def prune_auto_snapshots(install_path: str | Path, keep: int = AUTO_SNAPSHOT_LIMIT) -> int:
    """Remove old auto snapshots beyond the retention limit. Returns count deleted."""
    entries = list_snapshots(install_path)
    auto = [
        e for e in entries
        if e["snapshot"].get("trigger") in ("boot", "restart")
        and not e["snapshot"].get("label")
    ]
    if len(auto) <= keep:
        return 0
    to_delete = auto[keep:]
    deleted = 0
    for entry in to_delete:
        try:
            delete_snapshot(install_path, entry["filename"])
            deleted += 1
        except Exception:
            pass
    return deleted


def _deduplicate_restart_snapshot(
    install_path: str | Path, just_saved: str
) -> str | None:
    """Remove the previous intermediate restart snapshot if it matches.

    Mirrors snapshots.ts deduplicateRestartSnapshot.
    """
    from .nodes import node_key

    entries = list_snapshots(install_path)
    saved_idx = next(
        (i for i, e in enumerate(entries) if e["filename"] == just_saved), -1
    )
    if saved_idx < 0 or saved_idx >= len(entries) - 1:
        return None

    saved = entries[saved_idx]
    prev = entries[saved_idx + 1]
    ps = prev["snapshot"]

    if ps.get("trigger") != "restart" or ps.get("label"):
        return None

    ss = saved["snapshot"]
    # ComfyUI version must match
    if (ps["comfyui"].get("ref") != ss["comfyui"].get("ref")
            or ps["comfyui"].get("commit") != ss["comfyui"].get("commit")):
        return None

    # Custom nodes must match exactly
    prev_nodes = ps.get("customNodes", [])
    saved_nodes = ss.get("customNodes", [])
    if len(prev_nodes) != len(saved_nodes):
        return None
    prev_map = {node_key(n): n for n in prev_nodes}
    for sn in saved_nodes:
        pn = prev_map.get(node_key(sn))
        if pn is None:
            return None
        if (pn.get("type") != sn.get("type") or pn.get("version") != sn.get("version")
                or pn.get("commit") != sn.get("commit") or pn.get("enabled") != sn.get("enabled")):
            return None

    # Previous is intermediate — remove it
    delete_snapshot(install_path, prev["filename"])
    return prev["filename"]


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_snapshots(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Diff two snapshots: a (older/base) → b (newer/target).

    Returns a SnapshotDiff dict. Mirrors snapshots.ts diffSnapshots.
    """
    from .nodes import node_key

    diff: dict[str, Any] = {
        "comfyuiChanged": False,
        "updateChannelChanged": False,
        "nodesAdded": [],
        "nodesRemoved": [],
        "nodesChanged": [],
        "pipsAdded": [],
        "pipsRemoved": [],
        "pipsChanged": [],
    }

    # ComfyUI version
    ac, bc = a.get("comfyui", {}), b.get("comfyui", {})
    if ac.get("ref") != bc.get("ref") or ac.get("commit") != bc.get("commit"):
        diff["comfyuiChanged"] = True
        diff["comfyui"] = {
            "from": {"ref": ac.get("ref"), "commit": ac.get("commit")},
            "to": {"ref": bc.get("ref"), "commit": bc.get("commit")},
        }

    # Update channel
    a_ch = a.get("updateChannel", "stable")
    b_ch = b.get("updateChannel", "stable")
    if a_ch != b_ch:
        diff["updateChannelChanged"] = True
        diff["updateChannel"] = {"from": a_ch, "to": b_ch}

    # Custom nodes
    a_nodes = {node_key(n): n for n in a.get("customNodes", [])}
    b_nodes = {node_key(n): n for n in b.get("customNodes", [])}

    for key, bn in b_nodes.items():
        an = a_nodes.get(key)
        if an is None:
            diff["nodesAdded"].append(bn)
        elif (an.get("version") != bn.get("version") or an.get("commit") != bn.get("commit")
              or an.get("enabled") != bn.get("enabled") or an.get("type") != bn.get("type")):
            diff["nodesChanged"].append({
                "id": bn.get("id", ""),
                "type": bn.get("type", ""),
                "from": {"version": an.get("version"), "commit": an.get("commit"), "enabled": an.get("enabled")},
                "to": {"version": bn.get("version"), "commit": bn.get("commit"), "enabled": bn.get("enabled")},
            })
    for key, an in a_nodes.items():
        if key not in b_nodes:
            diff["nodesRemoved"].append(an)

    # Pip packages
    a_pips = a.get("pipPackages", {})
    b_pips = b.get("pipPackages", {})
    for name, ver in b_pips.items():
        if name not in a_pips:
            diff["pipsAdded"].append({"name": name, "version": ver})
        elif a_pips[name] != ver:
            diff["pipsChanged"].append({"name": name, "from": a_pips[name], "to": ver})
    for name in a_pips:
        if name not in b_pips:
            diff["pipsRemoved"].append({"name": name, "version": a_pips[name]})

    return diff


def diff_against_current(
    install_path: str | Path,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Capture current state and diff against a target snapshot."""
    current = capture_state(install_path)
    # Wrap as a full snapshot for diffing
    current_snap = {
        "version": 1,
        "createdAt": _iso_now(),
        "trigger": "manual",
        "label": None,
        **current,
    }
    return diff_snapshots(current_snap, target)


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

def build_export_envelope(
    installation_name: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a SnapshotExportEnvelope from snapshot entries."""
    return {
        "type": "comfyui-desktop-2-snapshot",
        "version": 1,
        "exportedAt": _iso_now(),
        "installationName": installation_name,
        "snapshots": [e["snapshot"] for e in entries],
    }


def _is_valid_custom_node(n: Any) -> bool:
    if not isinstance(n, dict):
        return False
    dir_name = n.get("dirName") or n.get("dir_name", "")
    if not dir_name or dir_name != os.path.basename(dir_name) or dir_name in (".", ".."):
        return False
    if not isinstance(n.get("id"), str) or not n["id"]:
        return False
    if n.get("type") not in ("cnr", "git", "file"):
        return False
    return True


def _is_valid_snapshot(s: Any) -> bool:
    if not isinstance(s, dict):
        return False
    if s.get("version") != 1:
        return False
    if not isinstance(s.get("createdAt"), str):
        return False
    try:
        _parse_iso(s["createdAt"])
    except (ValueError, TypeError):
        return False
    if s.get("trigger") not in VALID_TRIGGERS:
        return False
    if not isinstance(s.get("comfyui"), dict):
        return False
    if not isinstance(s.get("customNodes"), list):
        return False
    if not isinstance(s.get("pipPackages"), dict):
        return False
    for node in s["customNodes"]:
        if not _is_valid_custom_node(node):
            return False
    for name, ver in s["pipPackages"].items():
        if not VALID_PIP_NAME.match(name):
            return False
        if not isinstance(ver, str):
            return False
    return True


def validate_export_envelope(data: Any) -> dict[str, Any]:
    """Validate an import file. Returns the validated envelope or raises."""
    if not isinstance(data, dict):
        raise ValueError("Invalid file: not a JSON object")
    if data.get("type") != "comfyui-desktop-2-snapshot":
        raise ValueError("Invalid file: not a ComfyUI Desktop 2.0 snapshot export")
    if data.get("version") != 1:
        raise ValueError(f"Unsupported snapshot version: {data.get('version')}")
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, list) or len(snapshots) == 0:
        raise ValueError("File contains no snapshots")
    for i, s in enumerate(snapshots):
        if not _is_valid_snapshot(s):
            raise ValueError(f"Invalid snapshot at index {i}")
    return data


def import_snapshots(
    install_path: str | Path,
    envelope: dict[str, Any],
) -> dict[str, int]:
    """Import snapshots from an export envelope. Returns {imported, skipped}."""
    snap_dir = _snapshots_dir(install_path)
    snap_dir.mkdir(parents=True, exist_ok=True)

    existing = list_snapshots(install_path)
    existing_keys = {
        f"{e['snapshot']['createdAt']}|{e['snapshot']['trigger']}" for e in existing
    }

    imported = 0
    skipped = 0
    for snapshot in envelope["snapshots"]:
        key = f"{snapshot['createdAt']}|{snapshot['trigger']}"
        if key in existing_keys:
            skipped += 1
            continue

        dt = _parse_iso(snapshot["createdAt"])
        suffix = os.urandom(3).hex()
        filename = f"{_format_timestamp(dt)}-{snapshot['trigger']}-{suffix}.json"
        file_path = snap_dir / filename
        tmp_path = file_path.with_suffix(f".{suffix}.tmp")
        tmp_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        tmp_path.rename(file_path)
        existing_keys.add(key)
        imported += 1

    return {"imported": imported, "skipped": skipped}


def resolve_snapshot_id(install_path: str | Path, snapshot_id: str) -> str:
    """Resolve a snapshot ID (filename, #index, or partial match).

    Supports:
    - Direct filename ending in .json
    - Numeric index like #1 (newest), #2, etc.
    - Partial filename match (must be unambiguous)
    """
    if snapshot_id.endswith(".json"):
        return snapshot_id

    if snapshot_id.startswith("#"):
        try:
            idx = int(snapshot_id[1:]) - 1
        except ValueError:
            raise ValueError(f"Invalid snapshot index: {snapshot_id}")
        entries = list_snapshots(install_path)
        if idx < 0 or idx >= len(entries):
            raise ValueError(f"Snapshot index {snapshot_id} out of range (have {len(entries)})")
        return entries[idx]["filename"]

    entries = list_snapshots(install_path)
    matches = [e for e in entries if snapshot_id in e["filename"]]
    if len(matches) == 1:
        return matches[0]["filename"]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous snapshot ID '{snapshot_id}' — matches {len(matches)} snapshots")
    raise ValueError(f"Snapshot not found: {snapshot_id}")


def export_snapshot(
    install_path: str | Path,
    filename: str,
    dest_path: str | Path,
    installation_name: str = "unknown",
) -> None:
    """Export a single snapshot to a file."""
    snapshot = load_snapshot(install_path, filename)
    envelope = {
        "type": "comfyui-desktop-2-snapshot",
        "version": 1,
        "exportedAt": _iso_now(),
        "installationName": installation_name,
        "snapshots": [snapshot],
    }
    from safe_file import atomic_write
    atomic_write(Path(dest_path), json.dumps(envelope, indent=2))


# ---------------------------------------------------------------------------
# Restore — Custom Nodes
# ---------------------------------------------------------------------------

def _is_manager_node(node_id: str) -> bool:
    return "comfyui-manager" in node_id.lower()


def restore_custom_nodes(
    install_path: str | Path,
    target_snapshot: dict[str, Any],
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore custom nodes to match a target snapshot.

    Mirrors snapshots.ts restoreCustomNodes. Returns a result dict with
    installed/removed/switched/enabled/disabled/skipped/failed lists.
    """
    from safe_file import is_safe_path_component as _is_safe_path_component
    from .nodes import (
        scan_custom_nodes, node_key, add_cnr_node, add_git_node,
        enable_node, disable_node, remove_node, _run_post_install,
    )
    from .git_utils import is_git_available, git_fetch_and_checkout

    install_path = Path(install_path)
    comfyui_dir = install_path / "ComfyUI"
    custom_nodes_dir = comfyui_dir / "custom_nodes"

    result: dict[str, list[Any]] = {
        "installed": [], "removed": [], "switched": [],
        "enabled": [], "disabled": [], "skipped": [], "failed": [],
    }

    out = send_output or (lambda _: None)

    # 1. Scan current state
    current_nodes = scan_custom_nodes(install_path)
    current_map = {node_key(n): n for n in current_nodes}
    target_nodes = target_snapshot.get("customNodes", [])
    target_map = {node_key(n): n for n in target_nodes}

    git_available = is_git_available()
    if not git_available:
        out("⚠ git not found in PATH — git node operations will be skipped\n")

    nodes_needing_post_install: list[Path] = []

    # 2. Remove extra nodes (present in current but not in target)
    for key, cn in current_map.items():
        if key in target_map:
            continue
        if _is_manager_node(cn.get("id", "")):
            result["skipped"].append(cn["id"])
            continue
        dir_name = cn.get("dir_name") or cn.get("dirName", "")
        if not dir_name or not _is_safe_path_component(dir_name):
            continue
        try:
            remove_node(install_path, dir_name, send_output=send_output)
            result["removed"].append(cn["id"])
            out(f"Removed {cn['id']}\n")
        except Exception as e:
            result["failed"].append({"id": cn["id"], "error": str(e)})

    # 3. Process target nodes
    for tn in target_nodes:
        node_id = tn.get("id", "")
        if _is_manager_node(node_id):
            result["skipped"].append(node_id)
            continue

        dir_name = tn.get("dirName") or tn.get("dir_name", "")
        if not dir_name or not _is_safe_path_component(dir_name):
            result["failed"].append({"id": node_id, "error": "unsafe dirName"})
            continue

        nk = node_key(tn)
        current_node = current_map.get(nk)

        # Install missing nodes
        if current_node is None:
            node_type = tn.get("type", "")
            if node_type == "cnr":
                try:
                    add_cnr_node(install_path, node_id, version=tn.get("version"),
                                 send_output=send_output)
                    result["installed"].append(node_id)
                    nodes_needing_post_install.append(custom_nodes_dir / dir_name)
                    if not tn.get("enabled"):
                        disable_node(install_path, dir_name, send_output=send_output)
                except Exception as e:
                    result["failed"].append({"id": node_id, "error": str(e)})
            elif node_type == "git":
                url = tn.get("url")
                if not url:
                    result["failed"].append({"id": node_id, "error": "no URL for git node"})
                    continue
                if not git_available:
                    result["failed"].append({"id": node_id, "error": "git not available"})
                    continue
                try:
                    add_git_node(install_path, url, send_output=send_output)
                    # Checkout specific commit if available
                    commit = tn.get("commit")
                    if commit:
                        node_path = custom_nodes_dir / dir_name
                        rc = git_fetch_and_checkout(str(node_path), commit, send_output)
                        if rc != 0:
                            out(f"⚠ Could not checkout commit {commit[:7]} for {node_id}\n")
                    result["installed"].append(node_id)
                    nodes_needing_post_install.append(custom_nodes_dir / dir_name)
                    if not tn.get("enabled"):
                        disable_node(install_path, dir_name, send_output=send_output)
                except Exception as e:
                    result["failed"].append({"id": node_id, "error": str(e)})
            elif node_type == "file":
                out(f"⚠ Cannot auto-restore standalone file: {node_id}\n")
                result["failed"].append({"id": node_id, "error": "cannot auto-restore file nodes"})
            else:
                result["skipped"].append(node_id)
            continue

        # Enable/disable changes
        if not current_node.get("enabled") and tn.get("enabled"):
            try:
                enable_node(install_path, dir_name, send_output=send_output)
                result["enabled"].append(node_id)
                out(f"Enabled {node_id}\n")
            except Exception as e:
                result["failed"].append({"id": node_id, "error": f"enable failed: {e}"})
                continue
        elif current_node.get("enabled") and not tn.get("enabled"):
            try:
                disable_node(install_path, dir_name, send_output=send_output)
                result["disabled"].append(node_id)
                out(f"Disabled {node_id}\n")
            except Exception as e:
                result["failed"].append({"id": node_id, "error": f"disable failed: {e}"})
            continue

        # Version/commit changes (only if the node is/will be enabled)
        if tn.get("enabled") or current_node.get("enabled"):
            node_path = custom_nodes_dir / dir_name

            if tn.get("type") == "cnr" and tn.get("version") and current_node.get("version") != tn.get("version"):
                try:
                    add_cnr_node(install_path, node_id, version=tn["version"],
                                 send_output=send_output)
                    result["switched"].append(node_id)
                    nodes_needing_post_install.append(node_path)
                except Exception as e:
                    result["failed"].append({"id": node_id, "error": str(e)})
            elif tn.get("type") == "git" and tn.get("commit") and current_node.get("commit") != tn.get("commit"):
                if not git_available:
                    result["failed"].append({"id": node_id, "error": "git not available"})
                else:
                    rc = git_fetch_and_checkout(str(node_path), tn["commit"], send_output)
                    if rc == 0:
                        result["switched"].append(node_id)
                        nodes_needing_post_install.append(node_path)
                    else:
                        result["failed"].append({"id": node_id, "error": f"git checkout failed (exit {rc})"})
            else:
                result["skipped"].append(node_id)
        else:
            result["skipped"].append(node_id)

    # 4. Run post-install for installed/switched nodes
    if nodes_needing_post_install:
        for node_path in nodes_needing_post_install:
            out(f"\nRunning post-install for {node_path.name}…\n")
            _run_post_install(install_path, node_path, send_output=send_output)

    # 5. Install manager_requirements.txt if present (mirrors snapshots.ts)
    mgr_req = comfyui_dir / "manager_requirements.txt"
    if mgr_req.exists():
        from .pip_utils import install_filtered_requirements
        out("\nInstalling manager requirements…\n")
        try:
            rc = install_filtered_requirements(
                install_path, mgr_req, send_output=send_output,
            )
            if rc != 0:
                out(f"⚠ manager requirements install exited with code {rc}\n")
        except Exception as e:
            out(f"⚠ manager_requirements.txt failed: {e}\n")

    return result


# ---------------------------------------------------------------------------
# Restore — Pip Packages
# ---------------------------------------------------------------------------

def _find_site_packages(install_path: str | Path) -> Path | None:
    """Locate the site-packages directory for the active env."""
    from .environment import get_active_venv_dir, find_site_packages

    env_dir = get_active_venv_dir(install_path)
    if env_dir is None:
        return None
    return find_site_packages(env_dir)


def _normalize_dist_info_name(name: str) -> str:
    """PEP 503 normalization for dist-info matching."""
    return re.sub(r"[-_.]+", "_", name.lower())


def _find_dist_info_dir(site_packages: Path, package_name: str) -> str | None:
    """Find a package's dist-info directory."""
    normalized = _normalize_dist_info_name(package_name)
    try:
        for entry in site_packages.iterdir():
            if not entry.name.endswith(".dist-info"):
                continue
            stem = entry.name[:-len(".dist-info")]
            dash_idx = stem.find("-")
            if dash_idx < 0:
                continue
            dir_name = stem[:dash_idx]
            if _normalize_dist_info_name(dir_name) == normalized:
                return entry.name
    except OSError:
        pass
    return None


def _find_package_entries(site_packages: Path, package_name: str) -> list[str]:
    """Find all directories/files belonging to a package via RECORD."""
    entries: list[str] = []
    dist_info = _find_dist_info_dir(site_packages, package_name)
    if not dist_info:
        return entries
    entries.append(dist_info)

    record_path = site_packages / dist_info / "RECORD"
    try:
        content = record_path.read_text("utf-8")
        top_levels: set[str] = set()
        for line in content.splitlines():
            file_path = line.split(",")[0].strip()
            if not file_path or file_path.startswith(".."):
                continue
            top_level = file_path.replace("\\", "/").split("/")[0]
            if top_level and top_level != dist_info:
                top_levels.add(top_level)
        for tl in top_levels:
            if (site_packages / tl).exists():
                entries.append(tl)
    except OSError:
        # Fallback: common name patterns
        normalized = _normalize_dist_info_name(package_name)
        for suffix in ("", ".py", ".libs", ".data"):
            candidate = normalized + suffix
            if (site_packages / candidate).exists() and candidate not in entries:
                entries.append(candidate)

    return entries


def _create_targeted_backup(
    site_packages: Path,
    package_names: list[str],
) -> Path:
    """Back up specific packages from site-packages. Returns backup dir path."""
    backup_dir = site_packages.parent / f".restore-backup-{int(datetime.now().timestamp() * 1000)}"
    backup_dir.mkdir(parents=True)

    failures: list[str] = []
    for pkg in package_names:
        pkg_entries = _find_package_entries(site_packages, pkg)
        for entry in pkg_entries:
            src = site_packages / entry
            dst = backup_dir / entry
            try:
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            except Exception as e:
                failures.append(f"{entry}: {e}")

    if failures:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise RuntimeError(f"Backup failed for {len(failures)} entry(s): {'; '.join(failures)}")

    return backup_dir


def _restore_from_backup(backup_dir: Path, site_packages: Path) -> None:
    """Restore backed-up package files to site-packages."""
    try:
        for entry in backup_dir.iterdir():
            dst = site_packages / entry.name
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                else:
                    dst.unlink(missing_ok=True)
            if entry.is_dir():
                shutil.copytree(entry, dst)
            else:
                shutil.copy2(entry, dst)
    except Exception as e:
        import sys
        print(f"Failed to restore from backup: {e}", file=sys.stderr)


def restore_pip_packages(
    install_path: str | Path,
    target_snapshot: dict[str, Any],
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore pip packages to match a target snapshot.

    Mirrors snapshots.ts restorePipPackages with targeted backup/revert.
    Returns a RestoreResult dict.
    """
    from .pip_utils import pip_freeze, run_uv_pip, is_protected_package

    install_path = Path(install_path)
    out = send_output or (lambda _: None)

    result: dict[str, Any] = {
        "installed": [], "removed": [],
        "changed": [], "protectedSkipped": [],
        "failed": [], "errors": [],
    }

    # Check if pip sync should be skipped
    if target_snapshot.get("skipPipSync"):
        out("Pip sync skipped (skipPipSync flag set in snapshot)\n")
        return result

    # 1. Capture current pip state
    out("\nAnalyzing pip packages…\n")
    current_pips = pip_freeze(install_path)
    target_pips = target_snapshot.get("pipPackages", {})
    out(f"Found {len(current_pips)} current package(s), target snapshot has {len(target_pips)}\n")

    # 2. Compute what needs to change
    to_install: list[dict[str, str]] = []
    to_remove: list[str] = []

    for name, version in target_pips.items():
        if is_protected_package(name):
            if name not in current_pips or current_pips[name] != version:
                result["protectedSkipped"].append(name)
            continue
        # Skip non-standard versions
        if version.startswith("-e ") or "://" in version:
            continue
        if name not in current_pips:
            to_install.append({"name": name, "version": version})
        elif current_pips[name] != version:
            result["changed"].append({"name": name, "from": current_pips[name], "to": version})
            to_install.append({"name": name, "version": version})

    for name in current_pips:
        if name not in target_pips:
            if is_protected_package(name):
                result["protectedSkipped"].append(name)
            else:
                to_remove.append(name)

    # Track truly new packages for revert
    changed_names = {c["name"] for c in result["changed"]}
    new_pkg_names = [p["name"] for p in to_install if p["name"] not in changed_names]

    # Print plan
    parts: list[str] = []
    if new_pkg_names:
        parts.append(f"install {len(new_pkg_names)}")
    if result["changed"]:
        parts.append(f"change {len(result['changed'])}")
    if to_remove:
        parts.append(f"remove {len(to_remove)}")
    if result["protectedSkipped"]:
        parts.append(f"{len(result['protectedSkipped'])} protected (skipped)")
    if parts:
        out(f"\nPlan: {', '.join(parts)} package(s)\n\n")
    else:
        out("\nNo package changes needed\n")

    if not to_install and not to_remove:
        return result

    # 3. Create targeted backup
    site_packages = _find_site_packages(install_path)
    if not site_packages:
        raise RuntimeError("Could not locate site-packages directory")

    backup_pkgs = [p["name"] for p in to_install if p["name"] in changed_names] + to_remove
    backup_dir: Path | None = None
    if backup_pkgs:
        out("Creating backup of affected packages…\n")
        try:
            backup_dir = _create_targeted_backup(site_packages, backup_pkgs)
        except Exception as e:
            out(f"⚠ Backup failed: {e}\n")
            # Continue without backup

    try:
        # 4. Install missing + changed packages (bulk)
        if to_install:
            specs = [f"{p['name']}=={p['version']}" for p in to_install]
            out(f"Installing {len(specs)} package(s)…\n")
            rc = run_uv_pip(install_path, ["install"] + specs,
                            send_output=send_output)
            if rc == 0:
                for p in to_install:
                    if p["name"] in changed_names:
                        pass  # Already in result["changed"]
                    else:
                        result["installed"].append(p["name"])
            else:
                # Fallback: try one-by-one with --no-deps
                out(f"Bulk install failed (exit {rc}), trying one-by-one…\n")
                for p in to_install:
                    spec = f"{p['name']}=={p['version']}"
                    rc2 = run_uv_pip(install_path, ["install", "--no-deps", spec],
                                     send_output=send_output)
                    if rc2 == 0:
                        if p["name"] not in changed_names:
                            result["installed"].append(p["name"])
                    else:
                        result["failed"].append(p["name"])
                        result["errors"].append(f"Failed to install {spec}")

        # 5. Remove extras
        if to_remove:
            out(f"Removing {len(to_remove)} extra package(s)…\n")
            rc = run_uv_pip(install_path, ["uninstall"] + to_remove,
                            send_output=send_output)
            if rc == 0:
                result["removed"] = to_remove
            else:
                out(f"⚠ Bulk uninstall failed (exit {rc})\n")
                # Try one-by-one
                for name in to_remove:
                    rc2 = run_uv_pip(install_path, ["uninstall", name],
                                     send_output=send_output)
                    if rc2 == 0:
                        result["removed"].append(name)
                    else:
                        result["failed"].append(name)

        # 6. Revert if there were failures (mirrors TS behavior)
        if result["failed"]:
            out("\n⚠ Failures detected — reverting to pre-restore state…\n")
            if backup_dir and backup_dir.exists():
                _restore_from_backup(backup_dir, site_packages)
            if new_pkg_names:
                try:
                    run_uv_pip(install_path, ["uninstall"] + new_pkg_names,
                               send_output=send_output)
                except Exception:
                    pass
            result["installed"] = []
            result["removed"] = []
            result["changed"] = []
            result["errors"].append("Restore reverted to pre-restore state due to failures")

    except Exception as e:
        # Catastrophic failure — revert from backup
        result["errors"].append(str(e))
        if backup_dir and backup_dir.exists():
            out("\n⚠ Restore failed — reverting from backup…\n")
            _restore_from_backup(backup_dir, site_packages)
            if new_pkg_names:
                try:
                    run_uv_pip(install_path, ["uninstall"] + new_pkg_names,
                               send_output=send_output)
                except Exception:
                    pass
        raise
    finally:
        if backup_dir and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)

    return result


# ---------------------------------------------------------------------------
# Restore — ComfyUI Version
# ---------------------------------------------------------------------------

def _restore_comfyui_version(
    install_path: str | Path,
    target_snapshot: dict[str, Any],
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore ComfyUI to the target snapshot's commit.

    Mirrors snapshots.ts restoreComfyUIVersion.
    Returns {changed, commit, error?}.
    """
    import re as _re
    from .git_utils import git_fetch_and_checkout, read_git_head

    install_path = Path(install_path)
    comfyui_dir = install_path / "ComfyUI"
    out = send_output or (lambda _: None)

    target_commit = target_snapshot.get("comfyui", {}).get("commit")
    if not target_commit:
        return {"changed": False, "commit": None}

    if not _re.fullmatch(r"[a-f0-9]{7,40}", target_commit):
        return {"changed": False, "commit": None, "error": "Invalid commit hash in snapshot"}

    current_head = read_git_head(str(comfyui_dir))
    if current_head and (current_head.startswith(target_commit) or target_commit.startswith(current_head)):
        return {"changed": False, "commit": current_head}

    git_dir = comfyui_dir / ".git"
    if not git_dir.exists():
        msg = "ComfyUI .git directory not found — cannot restore version"
        out(f"⚠ {msg}\n")
        return {"changed": False, "commit": current_head, "error": msg}

    out(f"Checking out ComfyUI commit {target_commit[:7]}…\n")
    rc = git_fetch_and_checkout(str(comfyui_dir), target_commit, send_output)
    if rc != 0:
        msg = f"git checkout failed with exit code {rc}"
        out(f"⚠ {msg}\n")
        return {"changed": False, "commit": current_head, "error": msg}

    new_head = read_git_head(str(comfyui_dir))
    return {"changed": True, "commit": new_head}


# ---------------------------------------------------------------------------
# Full Restore (nodes + pip)
# ---------------------------------------------------------------------------

def restore_snapshot(
    install_path: str | Path,
    filename: str,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full snapshot restore: nodes first, then pip.

    Mirrors the snapshot-restore action handler in standalone.ts.
    Returns a combined result dict.
    """
    out = send_output or (lambda _: None)

    target = load_snapshot(install_path, filename)

    out(f"Restoring snapshot: {filename}\n")
    out(f"Trigger: {target.get('trigger', '?')}")
    label = target.get("label")
    if label:
        out(f" ({label})")
    out("\n\n")

    # Phase 0: ComfyUI version
    comfy_result = _restore_comfyui_version(
        install_path, target, send_output=send_output
    )

    # Phase 1: Custom nodes
    out("═══ Phase 1: Custom Nodes ═══\n\n")
    node_result = restore_custom_nodes(
        install_path, target, send_output=send_output
    )

    # Phase 2: Pip packages
    out("\n═══ Phase 2: Pip Packages ═══\n")
    pip_result: dict[str, Any] = {
        "installed": [], "removed": [], "changed": [],
        "protectedSkipped": [], "failed": [], "errors": [],
    }
    pip_error = False
    try:
        pip_result = restore_pip_packages(
            install_path, target, send_output=send_output
        )
    except Exception as e:
        pip_error = True
        pip_result["errors"].append(str(e))
        out(f"\n⚠ Pip restore failed: {e}\n")

    # Save post-restore snapshot
    out("\nSaving post-restore snapshot…\n")
    try:
        post_filename = save_snapshot(
            install_path, trigger="post-restore", label="after-restore"
        )
        out(f"Saved: {post_filename}\n")
    except Exception as e:
        out(f"⚠ Post-restore snapshot failed: {e}\n")

    # Build summary
    combined = {
        "comfyui": comfy_result,
        "nodes": node_result,
        "pip": pip_result,
        "pipReverted": pip_error,
    }

    out("\n═══ Summary ═══\n")
    if comfy_result.get("changed"):
        out(f"ComfyUI: checked out {comfy_result.get('commit', '?')[:12]}\n")
    elif comfy_result.get("error"):
        out(f"ComfyUI: ⚠ {comfy_result['error']}\n")
    n = node_result
    out(f"Nodes: {len(n['installed'])} installed, {len(n['removed'])} removed, "
        f"{len(n['switched'])} switched, {len(n['enabled'])} enabled, "
        f"{len(n['disabled'])} disabled, {len(n['failed'])} failed\n")
    p = pip_result
    out(f"Pip:   {len(p['installed'])} installed, {len(p['removed'])} removed, "
        f"{len(p['changed'])} changed, {len(p['protectedSkipped'])} protected (skipped), "
        f"{len(p['failed'])} failed\n")
    if pip_error:
        out("⚠ Pip restore was reverted due to errors\n")

    return combined
