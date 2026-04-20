"""Custom node management — mirrors ComfyUI-Launcher nodes.ts, cnr.ts, snapshots.ts."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests

from .git_utils import git_clone, read_git_head, read_git_remote_url, _resolve_git_dir

CNR_API_URL = "https://api.comfy.org/nodes"

TRACKING_FILE = ".tracking"

_SKIP_PREFIXES = (".", "__pycache__")


# ---------------------------------------------------------------------------
# Path validation — mirrors cnr.ts isSafePathComponent
# ---------------------------------------------------------------------------

def _is_safe_path_component(name: str) -> bool:
    return bool(name) and name == Path(name).name and name not in (".", "..")


# ---------------------------------------------------------------------------
# Scanning — mirrors nodes.ts scanCustomNodes / identifyNode / nodeKey
# ---------------------------------------------------------------------------

def node_key(node: dict[str, Any]) -> str:
    """Return '{type}:{dirName}' key for a scanned node.

    Accepts both snake_case (dir_name, from Python scanner) and camelCase
    (dirName, from Desktop 2.0 snapshot JSON) for cross-compatibility.
    """
    dir_name = node.get("dir_name") or node.get("dirName", "")
    return f"{node['type']}:{dir_name}"


def identify_node(node_path: Path) -> dict[str, Any]:
    """Identify a custom node directory.

    CNR check: .tracking file exists → read pyproject.toml for name/version.
    Git check: .git exists → read HEAD commit + origin URL.
    Fallback: type='git' without metadata.
    """
    dir_name = node_path.name
    result: dict[str, Any] = {"dir_name": dir_name}

    # CNR node — identified by .tracking file
    tracking_file = node_path / ".tracking"
    if tracking_file.exists():
        result["type"] = "cnr"
        result["id"] = dir_name
        # Read name/version from pyproject.toml (mirrors nodes.ts readTomlProjectField)
        pyproject = node_path / "pyproject.toml"
        if pyproject.exists():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                project = data.get("project", {})
                name = project.get("name")
                if isinstance(name, str) and name:
                    result["id"] = name
                ver = project.get("version")
                if isinstance(ver, str) and ver:
                    result["version"] = ver
            except (OSError, tomllib.TOMLDecodeError):
                pass
        return result

    # Git node — identified by .git dir/file
    git_dir = _resolve_git_dir(node_path)
    if git_dir is not None:
        result["type"] = "git"
        result["id"] = dir_name
        commit = read_git_head(str(node_path))
        if commit:
            result["commit"] = commit
        url = read_git_remote_url(str(node_path))
        if url:
            result["url"] = url
        return result

    # File / unknown — treat as file type for plain dirs, git for .py files
    if node_path.is_file():
        result["type"] = "file"
        result["id"] = dir_name
    else:
        result["type"] = "git"
        result["id"] = dir_name
    return result


def scan_custom_nodes(install_path: str | Path) -> list[dict[str, Any]]:
    """Scan custom_nodes/ and custom_nodes/.disabled/ for installed nodes.

    Returns a list of node dicts matching the ScannedNode shape.
    Mirrors nodes.ts scanCustomNodes.
    """
    install_path = Path(install_path)
    custom_nodes_dir = install_path / "ComfyUI" / "custom_nodes"
    return scan_custom_nodes_dir(custom_nodes_dir)


def scan_custom_nodes_dir(custom_nodes_dir: str | Path) -> list[dict[str, Any]]:
    """Scan a custom_nodes directory directly.

    Like scan_custom_nodes but takes the custom_nodes path itself, making it
    usable for manual/portable installs where the ComfyUI dir IS the root.
    """
    custom_nodes_dir = Path(custom_nodes_dir)
    nodes: list[dict[str, Any]] = []

    # Active nodes
    if custom_nodes_dir.exists():
        for entry in sorted(custom_nodes_dir.iterdir()):
            if any(entry.name.startswith(p) for p in _SKIP_PREFIXES):
                continue
            if entry.is_dir() or (entry.is_file() and entry.suffix == ".py"):
                node = identify_node(entry)
                node["enabled"] = True
                nodes.append(node)

    # Disabled nodes
    disabled_dir = custom_nodes_dir / ".disabled"
    if disabled_dir.exists():
        for entry in sorted(disabled_dir.iterdir()):
            if any(entry.name.startswith(p) for p in _SKIP_PREFIXES):
                continue
            if entry.is_dir():
                node = identify_node(entry)
                node["enabled"] = False
                nodes.append(node)

    return nodes


# ---------------------------------------------------------------------------
# Post-install helper
# ---------------------------------------------------------------------------

def _run_post_install(
    install_path: str | Path,
    node_path: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Run post-install scripts for a custom node.

    1. If requirements.txt exists, run install_filtered_requirements.
    2. If install.py exists, run it with the env python.
    """
    from .pip_utils import install_filtered_requirements
    from .environment import get_active_python_path

    req_file = node_path / "requirements.txt"
    if req_file.exists():
        if send_output:
            send_output(f"Installing requirements for {node_path.name}...\n")
        rc = install_filtered_requirements(
            install_path, req_file, send_output=send_output
        )
        if rc != 0 and send_output:
            send_output(f"⚠ pip install exited with code {rc}\n")

    install_script = node_path / "install.py"
    if install_script.exists():
        if send_output:
            send_output(f"Running install.py for {node_path.name}...\n")
        python = get_active_python_path(Path(install_path))
        if not python:
            if send_output:
                send_output("⚠ Python not found, skipping install.py\n")
            return
        proc = subprocess.Popen(
            [str(python), str(install_script)],
            cwd=str(node_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if send_output:
                send_output(line)
        proc.wait()
        if proc.returncode != 0 and send_output:
            send_output(f"⚠ install.py exited with code {proc.returncode}\n")


# ---------------------------------------------------------------------------
# Install — git nodes
# ---------------------------------------------------------------------------

def add_git_node(
    install_path: str | Path,
    url: str,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Clone a git custom node into custom_nodes/.

    Returns the scanned node dict.
    """
    install_path = Path(install_path)
    custom_nodes_dir = install_path / "ComfyUI" / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)

    # Derive directory name from URL
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    if not _is_safe_path_component(name):
        raise RuntimeError(f"Unsafe node name derived from URL: {name!r}")

    dest = custom_nodes_dir / name
    if dest.exists():
        raise RuntimeError(f"Node directory already exists: {dest}")

    if send_output:
        send_output(f"Cloning {url} -> {name}...\n")

    rc = git_clone(url, str(dest), send_output)
    if rc != 0:
        # Clean up partial clone
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git clone failed with exit code {rc}")

    # Post-install
    _run_post_install(install_path, dest, send_output=send_output)

    node = identify_node(dest)
    node["enabled"] = True

    if send_output:
        send_output(f"✓ Added git node: {name}\n")

    return node


# ---------------------------------------------------------------------------
# Install — CNR nodes
# ---------------------------------------------------------------------------

def get_cnr_install_info(
    node_id: str,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Query the CNR API for download info.

    GET {CNR_API_URL}/{node_id}/install?version={version}
    Returns dict with downloadUrl and version, or None on failure.
    """
    params: dict[str, str] = {}
    if version:
        params["version"] = version
    try:
        resp = requests.get(
            f"{CNR_API_URL}/{node_id}/install",
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def add_cnr_node(
    install_path: str | Path,
    node_id: str,
    version: str | None = None,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Install a CNR node: download ZIP, extract, write .tracking, post-install.

    If the node already exists, switches to the requested version
    (mirrors cnr.ts switchCnrVersion).

    Returns the scanned node dict.
    """
    if not _is_safe_path_component(node_id):
        raise RuntimeError(f"Unsafe CNR node ID: {node_id!r}")

    install_path = Path(install_path)
    custom_nodes_dir = install_path / "ComfyUI" / "custom_nodes"
    custom_nodes_dir.mkdir(parents=True, exist_ok=True)

    dest = custom_nodes_dir / node_id
    is_update = dest.exists()

    if send_output:
        action = "Updating" if is_update else "Installing"
        send_output(f"{action} CNR node {node_id}...\n")

    info = get_cnr_install_info(node_id, version)
    if not info:
        raise RuntimeError(f"Failed to get install info for CNR node: {node_id}")

    download_url = info.get("downloadUrl") or info.get("download_url")
    resolved_version = info.get("version")
    if not download_url:
        raise RuntimeError(f"No download URL returned for CNR node: {node_id}")

    if send_output:
        ver_str = f" v{resolved_version}" if resolved_version else ""
        send_output(f"Downloading {node_id}{ver_str}...\n")

    # Read old tracking for cleanup (if updating)
    old_files: set[str] = set()
    if is_update:
        tracking_path = dest / TRACKING_FILE
        try:
            for line in tracking_path.read_text(encoding="utf-8").splitlines():
                trimmed = line.strip()
                if trimmed:
                    old_files.add(trimmed)
        except OSError:
            pass

    # Download ZIP and extract to temp dir first (mirrors switchCnrVersion)
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = Path(tmp_dir) / f"{node_id}.zip"
        tmp_extract = Path(tmp_dir) / "extract"

        resp = requests.get(download_url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        if send_output:
            send_output(f"Extracting to {dest.name}/...\n")

        # Extract to temp first to get the true new file list
        tmp_extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            _safe_extractall(zf, tmp_extract)

        # Walk the extracted dir to get files-only list (mirrors cnr.ts walkDir)
        new_files = _walk_dir(tmp_extract)
        new_file_set = set(new_files)

        # Copy extracted files into destination (overwriting existing)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tmp_extract, dest, dirs_exist_ok=True)

    # Clean up garbage files from old version (mirrors switchCnrVersion)
    if old_files:
        garbage_dirs: set[str] = set()
        for old_file in old_files:
            if old_file not in new_file_set:
                # Remove the stale file
                stale_path = dest / Path(*old_file.split("/"))
                try:
                    stale_path.unlink()
                except OSError:
                    pass
                # Track parent dirs for cleanup
                parts = old_file.split("/")
                for i in range(1, len(parts)):
                    garbage_dirs.add("/".join(parts[:i]))

        # Remove empty garbage directories (deepest first)
        for dir_rel in sorted(garbage_dirs, key=len, reverse=True):
            try:
                (dest / Path(*dir_rel.split("/"))).rmdir()
            except OSError:
                pass

    # Write .tracking file (newline-separated files-only, matches Desktop 2.0)
    tracking_file = dest / TRACKING_FILE
    from safe_file import atomic_write
    atomic_write(tracking_file, "\n".join(new_files) + "\n")

    # Post-install
    _run_post_install(install_path, dest, send_output=send_output)

    node = identify_node(dest)
    node["enabled"] = True

    action_past = "Updated" if is_update else "Added"
    if send_output:
        send_output(f"✓ {action_past} CNR node: {node_id}\n")

    return node


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------

def remove_node(
    install_path: str | Path,
    node_name: str,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Delete a custom node directory (active or disabled)."""
    if not _is_safe_path_component(node_name):
        raise RuntimeError(f"Unsafe node name: {node_name!r}")

    install_path = Path(install_path)
    custom_nodes_dir = install_path / "ComfyUI" / "custom_nodes"

    active = custom_nodes_dir / node_name
    disabled = custom_nodes_dir / ".disabled" / node_name

    target = None
    if active.exists():
        target = active
    elif disabled.exists():
        target = disabled
    else:
        raise RuntimeError(f"Node '{node_name}' not found.")

    if send_output:
        send_output(f"Removing {node_name}...\n")

    shutil.rmtree(target)

    if send_output:
        send_output(f"✓ Removed {node_name}\n")


# ---------------------------------------------------------------------------
# Enable / Disable — mirrors snapshots.ts enableNode / disableNode
# ---------------------------------------------------------------------------

def enable_node(
    install_path: str | Path,
    node_name: str,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Move a node from custom_nodes/.disabled/ → custom_nodes/.

    Mirrors snapshots.ts enableNode: removes destination if it already
    exists (e.g. from a previous crash) before renaming.
    """
    if not _is_safe_path_component(node_name):
        raise RuntimeError(f"Unsafe node name: {node_name!r}")

    install_path = Path(install_path)
    custom_nodes_dir = install_path / "ComfyUI" / "custom_nodes"
    src = custom_nodes_dir / ".disabled" / node_name
    dst = custom_nodes_dir / node_name

    if not src.exists():
        raise RuntimeError(f"Disabled node '{node_name}' not found.")

    # Remove destination collision (mirrors Desktop 2.0)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)

    shutil.move(str(src), str(dst))

    if send_output:
        send_output(f"✓ Enabled {node_name}\n")


def disable_node(
    install_path: str | Path,
    node_name: str,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Move a node from custom_nodes/ → custom_nodes/.disabled/.

    Mirrors snapshots.ts disableNode: removes destination if it already
    exists (e.g. from a previous crash) before renaming.
    """
    if not _is_safe_path_component(node_name):
        raise RuntimeError(f"Unsafe node name: {node_name!r}")

    install_path = Path(install_path)
    custom_nodes_dir = install_path / "ComfyUI" / "custom_nodes"
    src = custom_nodes_dir / node_name
    disabled_dir = custom_nodes_dir / ".disabled"
    dst = disabled_dir / node_name

    if not src.exists():
        raise RuntimeError(f"Node '{node_name}' not found.")

    disabled_dir.mkdir(parents=True, exist_ok=True)

    # Remove destination collision (mirrors Desktop 2.0)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)

    shutil.move(str(src), str(dst))

    if send_output:
        send_output(f"✓ Disabled {node_name}\n")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract a ZIP file safely, rejecting path traversal (Zip Slip).

    Validates that every extracted member resolves inside the target directory.
    """
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        member_path = (dest / member.filename).resolve()
        if not str(member_path).startswith(str(dest_resolved)):
            raise RuntimeError(
                f"Unsafe ZIP entry (path traversal): {member.filename!r}"
            )
    zf.extractall(dest)


def _walk_dir(directory: Path, base: str = "") -> list[str]:
    """Walk a directory and return relative file paths.

    Mirrors cnr.ts walkDir: returns files only (no directories),
    excludes the .tracking file itself.
    """
    results: list[str] = []
    try:
        for entry in sorted(directory.iterdir()):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                results.extend(_walk_dir(entry, rel))
            elif entry.name != TRACKING_FILE:
                results.append(rel)
    except OSError:
        pass
    return results


def _get_installation_record(install_path: Path) -> dict[str, Any] | None:
    """Find the installation record that matches the given path."""
    from .config import list_installations

    install_str = str(install_path)
    for _name, record in list_installations().items():
        if record.get("path") == install_str:
            return record
    return None
