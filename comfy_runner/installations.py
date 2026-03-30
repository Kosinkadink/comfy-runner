"""Multi-install registry — create, list, remove installations.

Mirrors ComfyUI-Launcher installations.ts.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .comfyui import clone_comfyui
from .config import (
    get_installation,
    get_installations_dir,
    list_installations,
    remove_installation as remove_installation_record,
    set_installation,
)
from .environment import (
    create_env,
    download_and_extract,
    fetch_latest_release,
    fetch_manifests,
    fetch_releases,
    get_variant_label,
    pick_variant,
    read_manifest,
    strip_master_packages,
)
from .git_utils import read_git_head


def init_installation(
    name: str = "main",
    variant: str | None = None,
    release_tag: str | None = None,
    install_dir: str | None = None,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Create a new ComfyUI installation from scratch.

    Steps (mirrors standalone.ts install + postInstall):
    1. Fetch release from GitHub
    2. Pick variant (auto-detect GPU or use explicit)
    3. Download + extract standalone environment
    4. Clone ComfyUI (checkout manifest's comfyui_ref)
    5. Create default venv via uv (inside ComfyUI/)
    6. Strip bulky packages from master env
    7. Register in config
    """
    existing = get_installation(name)
    if existing:
        raise RuntimeError(
            f"Installation '{name}' already exists at {existing.get('path', '?')}. "
            f"Use 'comfy-runner rm {name}' first, or choose a different --name."
        )

    # 1. Fetch release
    if send_output:
        send_output("Fetching available releases...\n")

    if release_tag:
        releases = fetch_releases(limit=30)
        release = next(
            (r for r in releases if r["tag_name"] == release_tag),
            None,
        )
        if not release:
            raise RuntimeError(
                f"Release '{release_tag}' not found. "
                f"Available: {[r['tag_name'] for r in releases[:10]]}"
            )
    else:
        release = fetch_latest_release()

    tag = release["tag_name"]
    if send_output:
        release_name = release.get("name") or tag
        send_output(f"Using release: {tag} ({release_name})\n")

    # 2. Fetch manifests and pick variant
    if send_output:
        send_output("Fetching variant manifests...\n")

    manifests = fetch_manifests(release)
    variant_data = pick_variant(manifests, release, variant_id=variant)
    variant_id = variant_data["variant_id"]
    manifest = variant_data["manifest"]
    download_files = variant_data["download_files"]

    if send_output:
        label = get_variant_label(variant_id)
        total_mb = sum(f.get("size", 0) for f in download_files) / 1048576
        send_output(f"Selected variant: {label} ({variant_id})\n")
        send_output(f"  ComfyUI ref: {manifest.get('comfyui_ref', '?')}\n")
        send_output(f"  Python: {manifest.get('python_version', '?')}\n")
        send_output(f"  Download size: {total_mb:.0f} MB\n\n")

    # 3. Determine install path
    if install_dir:
        install_path = Path(install_dir)
    else:
        install_path = get_installations_dir() / name

    # Clean up stale directory from a previous failed init (no config record
    # but directory exists on disk)
    if install_path.exists() and not existing:
        if send_output:
            send_output(f"Removing stale directory from previous failed init: {install_path}\n")
        shutil.rmtree(install_path, ignore_errors=True)

    install_path.mkdir(parents=True, exist_ok=True)

    try:
        # 4. Download + extract standalone environment
        if send_output:
            send_output("=== Downloading standalone environment ===\n")

        cache_key = f"{tag}_{variant_id}"
        download_and_extract(download_files, install_path, cache_key, send_output)

        # 4b. macOS binary repair (quarantine + codesigning)
        from .macos import repair_mac_binaries
        repair_mac_binaries(install_path, send_output)

        # 5. Clone ComfyUI (must happen before venv, which lives inside ComfyUI/)
        if send_output:
            send_output("\n=== Cloning ComfyUI ===\n")

        comfyui_ref = manifest.get("comfyui_ref")
        head_commit = clone_comfyui(install_path, ref=comfyui_ref, send_output=send_output)

        # 6. Create default venv
        if send_output:
            send_output("\n=== Creating default Python environment ===\n")

        create_env(install_path, send_output)

        # 7. Strip bulky packages from master env
        if send_output:
            send_output("\nCleaning up master environment...\n")

        strip_master_packages(install_path, send_output)
    except BaseException:
        # Clean up partially-created install directory
        if install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
            if send_output:
                send_output(f"\nCleaned up partial installation at {install_path}\n")
        raise

    # Read manifest.json from extracted environment (if present)
    disk_manifest = read_manifest(install_path)

    # 8. Register installation
    record: dict[str, Any] = {
        "path": str(install_path),
        "variant": variant_id,
        "release_tag": tag,
        "status": "installed",
        "comfyui_ref": comfyui_ref or "",
        "python_version": manifest.get("python_version", ""),
        "head_commit": head_commit or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "launch_args": "--enable-manager",
    }
    set_installation(name, record)

    if send_output:
        send_output(f"\n✓ Installation '{name}' created at {install_path}\n")

    return record


def remove(
    name: str,
    delete_files: bool = True,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Remove an installation (record + optionally files)."""
    record = get_installation(name)
    if not record:
        raise RuntimeError(f"Installation '{name}' not found.")

    if delete_files:
        install_path = Path(record["path"])
        if install_path.exists():
            if send_output:
                send_output(f"Deleting {install_path}...\n")
            shutil.rmtree(install_path)

    remove_installation_record(name)
    if send_output:
        send_output(f"✓ Installation '{name}' removed.\n")


def show_list() -> list[dict[str, Any]]:
    """Return all installations as a list of dicts with name included."""
    installations = list_installations()
    result = []
    for name, record in installations.items():
        result.append({"name": name, **record})
    return result
