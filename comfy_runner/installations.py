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
    build_standalone_env,
    create_env,
    download_and_extract,
    ensure_cuda_compatible_torch,
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
    cuda_compat: bool = False,
    max_cache_entries: int | None = None,
    build: bool = False,
    python_version: str | None = None,
    pbs_release: str | None = None,
    gpu: str | None = None,
    cuda_tag: str | None = None,
    torch_version: str | None = None,
    torch_spec: list[str] | None = None,
    torch_index_url: str | None = None,
    comfyui_ref: str | None = None,
) -> dict[str, Any]:
    """Create a new ComfyUI installation from scratch.

    When *build* is False (default), downloads a pre-built standalone
    environment from GitHub Releases (the original flow).

    When *build* is True, constructs the standalone environment locally
    from python-build-standalone + pip, allowing arbitrary Python/PyTorch
    version combinations.

    Steps (pre-built — mirrors standalone.ts install + postInstall):
    1. Fetch release from GitHub
    2. Pick variant (auto-detect GPU or use explicit)
    3. Download + extract standalone environment
    4. Clone ComfyUI (checkout manifest's comfyui_ref)
    5. Create default venv via uv (inside ComfyUI/)
    6. Strip bulky packages from master env
    7. Register in config

    Steps (ad-hoc build):
    1. Clone ComfyUI
    2. Download python-build-standalone + pip-install torch + deps + uv
    3. Create default venv via uv
    4. Register in config
    """
    existing = get_installation(name)
    if existing:
        raise RuntimeError(
            f"Installation '{name}' already exists at {existing.get('path', '?')}. "
            f"Use 'comfy-runner rm {name}' first, or choose a different --name."
        )

    # Determine install path
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

    if build:
        return _init_build(
            name=name,
            install_path=install_path,
            python_version=python_version or "3.13",
            pbs_release=pbs_release,
            gpu=gpu,
            cuda_tag=cuda_tag,
            torch_version=torch_version,
            torch_spec=torch_spec,
            torch_index_url=torch_index_url,
            comfyui_ref=comfyui_ref,
            send_output=send_output,
            cuda_compat=cuda_compat,
            max_cache_entries=max_cache_entries,
        )

    return _init_prebuilt(
        name=name,
        install_path=install_path,
        variant=variant,
        release_tag=release_tag,
        comfyui_ref=comfyui_ref,
        send_output=send_output,
        cuda_compat=cuda_compat,
        max_cache_entries=max_cache_entries,
    )


def _init_prebuilt(
    name: str,
    install_path: Path,
    variant: str | None,
    release_tag: str | None,
    comfyui_ref: str | None,
    send_output: Callable[[str], None] | None,
    cuda_compat: bool,
    max_cache_entries: int | None,
) -> dict[str, Any]:
    """Pre-built release flow (original init path)."""
    try:
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
        # 3. Download + extract standalone environment
        if send_output:
            send_output("=== Downloading standalone environment ===\n")

        cache_key = f"{tag}_{variant_id}"
        download_and_extract(
            download_files, install_path, cache_key, send_output,
            max_cache_entries=max_cache_entries,
        )

        # 3b. macOS binary repair (quarantine + codesigning)
        from .macos import repair_mac_binaries
        repair_mac_binaries(install_path, send_output)

        # 4. Clone ComfyUI (must happen before venv, which lives inside ComfyUI/)
        if send_output:
            send_output("\n=== Cloning ComfyUI ===\n")

        manifest_ref = comfyui_ref or manifest.get("comfyui_ref")
        head_commit = clone_comfyui(install_path, ref=manifest_ref, send_output=send_output)

        # 5. Create default venv
        if send_output:
            send_output("\n=== Creating default Python environment ===\n")

        create_env(install_path, send_output)

        # 5b. Ensure torch CUDA build matches host driver (hosted only)
        if cuda_compat:
            if send_output:
                send_output("\n=== Checking CUDA compatibility ===\n")
            ensure_cuda_compatible_torch(install_path, send_output)

        # 6. Strip bulky packages from master env
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

    # 7. Register installation
    record: dict[str, Any] = {
        "path": str(install_path),
        "variant": variant_id,
        "release_tag": tag,
        "status": "installed",
        "comfyui_ref": manifest_ref or "",
        "python_version": manifest.get("python_version", ""),
        "head_commit": head_commit or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "launch_args": "--enable-manager",
    }
    set_installation(name, record)

    if send_output:
        send_output(f"\nInstallation '{name}' created at {install_path}\n")

    return record


def _init_build(
    name: str,
    install_path: Path,
    python_version: str,
    pbs_release: str | None,
    gpu: str | None,
    cuda_tag: str | None,
    torch_version: str | None,
    torch_spec: list[str] | None,
    torch_index_url: str | None,
    comfyui_ref: str | None,
    send_output: Callable[[str], None] | None,
    cuda_compat: bool,
    max_cache_entries: int | None,
) -> dict[str, Any]:
    """Ad-hoc build flow — build standalone-env locally from scratch."""
    try:
        # 1. Clone ComfyUI first (build_standalone_env installs its deps)
        if send_output:
            send_output("=== Cloning ComfyUI ===\n")

        head_commit = clone_comfyui(install_path, ref=comfyui_ref, send_output=send_output)

        # 2. Build the standalone environment
        if send_output:
            send_output("\n")

        build_manifest = build_standalone_env(
            install_path=install_path,
            python_version=python_version,
            pbs_release=pbs_release,
            gpu=gpu,
            cuda_tag=cuda_tag,
            torch_version=torch_version,
            torch_spec=torch_spec,
            torch_index_url=torch_index_url,
            comfyui_ref=comfyui_ref,
            send_output=send_output,
            max_cache_entries=max_cache_entries,
        )

        # 3. Create default venv (same as pre-built flow)
        if send_output:
            send_output("\n=== Creating default Python environment ===\n")

        create_env(install_path, send_output)

        # 3b. Ensure torch CUDA build matches host driver
        if cuda_compat:
            if send_output:
                send_output("\n=== Checking CUDA compatibility ===\n")
            ensure_cuda_compatible_torch(install_path, send_output)

        # 4. Strip bulky packages from master env
        if send_output:
            send_output("\nCleaning up master environment...\n")

        strip_master_packages(install_path, send_output)
    except BaseException:
        if install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
            if send_output:
                send_output(f"\nCleaned up partial installation at {install_path}\n")
        raise

    # 5. Register installation
    record: dict[str, Any] = {
        "path": str(install_path),
        "variant": "adhoc-build",
        "release_tag": "adhoc",
        "status": "installed",
        "comfyui_ref": comfyui_ref or "",
        "python_version": build_manifest.get("python_version", python_version),
        "torch_version": build_manifest.get("torch_version", ""),
        "head_commit": head_commit or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "launch_args": "--enable-manager",
        "build_mode": "adhoc",
    }
    set_installation(name, record)

    if send_output:
        send_output(f"\nInstallation '{name}' created at {install_path}\n")

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
