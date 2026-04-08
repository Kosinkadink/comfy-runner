"""Shared deploy-mode logic used by both CLI and server.

Centralises the deploy decision tree and record mutation so that CLI
and server stay in sync when new modes are added.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .comfyui import deploy_pr, deploy_ref, deploy_reset


def deploy_latest(
    install_path: str | Path,
    record: dict[str, Any],
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Update ComfyUI to the ref specified by the latest standalone release.

    Only updates the git checkout — does NOT re-download the standalone
    environment (Python, torch, etc.).  Fails fast if the latest release
    requires a different Python version than the current install.
    """
    from .environment import fetch_latest_release, fetch_manifests

    if send_output:
        send_output("Fetching latest standalone release...\n")

    release = fetch_latest_release()
    tag = release["tag_name"]

    if send_output:
        send_output(f"Latest release: {tag}\n")

    manifests = fetch_manifests(release)

    # Match the variant this install was created with
    variant_id = record.get("variant")
    if not variant_id:
        raise RuntimeError(
            "No variant recorded for this installation. Cannot resolve manifest."
        )

    manifest = next((m for m in manifests if m["id"] == variant_id), None)
    if not manifest:
        raise RuntimeError(
            f"Variant '{variant_id}' not found in release {tag}. "
            f"Available: {[m['id'] for m in manifests]}"
        )

    comfyui_ref = manifest.get("comfyui_ref")
    if not comfyui_ref:
        raise RuntimeError(f"Manifest for '{variant_id}' has no comfyui_ref.")

    # Guard: refuse if Python version changed (env update required)
    new_py = manifest.get("python_version", "")
    current_py = record.get("python_version", "")
    if current_py and new_py and current_py != new_py:
        raise RuntimeError(
            f"Latest release requires Python {new_py}, but this install "
            f"has {current_py}. A standalone env update is needed first."
        )

    if send_output:
        send_output(f"ComfyUI ref: {comfyui_ref}\n")

    result = deploy_ref(install_path, comfyui_ref, fetch_first=True, send_output=send_output)
    result["release_tag"] = tag
    result["comfyui_ref"] = comfyui_ref
    return result


def deploy_pull(
    install_path: str | Path,
    record: dict[str, Any],
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Re-fetch the currently tracked PR or branch.

    Raises RuntimeError if no movable target is tracked.
    """
    deployed_pr = record.get("deployed_pr")
    deployed_branch = record.get("deployed_branch")

    if deployed_pr:
        if send_output:
            send_output(f"Pulling latest for PR #{deployed_pr}...\n")
        return deploy_pr(install_path, int(deployed_pr), send_output=send_output)
    elif deployed_branch:
        if send_output:
            send_output(f"Pulling latest for branch '{deployed_branch}'...\n")
        return deploy_ref(install_path, deployed_branch, send_output=send_output)
    else:
        raise RuntimeError(
            "No tracked PR or branch for --pull. "
            "Use --branch or --pr first to set a tracking target."
        )


def execute_deploy(
    install_path: str | Path,
    record: dict[str, Any],
    *,
    pr: int | None = None,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    reset: bool = False,
    latest: bool = False,
    pull: bool = False,
    repo_url: str | None = None,
    send_output: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the appropriate deploy action and return (result, record_updates).

    *record_updates* contains only the fields that should be merged into
    the persisted installation record.
    """
    # --- execute the deploy mode ---
    if latest:
        result = deploy_latest(install_path, record, send_output=send_output)
    elif pull:
        result = deploy_pull(install_path, record, send_output=send_output)
    elif reset:
        original_ref = record.get("comfyui_ref")
        if not original_ref:
            raise RuntimeError(
                "No original comfyui_ref recorded for this installation. "
                "Cannot reset."
            )
        result = deploy_reset(install_path, original_ref, send_output=send_output)
    elif pr:
        result = deploy_pr(install_path, int(pr), send_output=send_output)
    elif branch:
        result = deploy_ref(
            install_path, branch, repo_url=repo_url, send_output=send_output
        )
    elif tag:
        result = deploy_ref(install_path, tag, send_output=send_output)
    elif commit:
        result = deploy_ref(
            install_path, commit, fetch_first=False, send_output=send_output
        )
    else:
        raise RuntimeError(
            "Specify one of: --pr, --branch, --tag, --commit, --reset, --latest, or --pull"
        )

    # --- build record updates ---
    updates: dict[str, Any] = {}

    if result.get("new_head"):
        updates["head_commit"] = result["new_head"]

    if pr:
        updates["deployed_pr"] = int(pr)
        updates["deployed_branch"] = None
    elif branch:
        updates["deployed_branch"] = branch
        updates["deployed_pr"] = None
        updates["deployed_repo"] = None
        updates["deployed_title"] = None
    elif pull:
        pass  # keep existing tracking unchanged
    else:
        # tag, commit, reset, latest — clear movable tracking
        updates["deployed_pr"] = None
        updates["deployed_branch"] = None
        updates["deployed_repo"] = None
        updates["deployed_title"] = None

    # latest also updates the baseline
    if latest:
        updates["release_tag"] = result.get("release_tag")
        updates["comfyui_ref"] = result.get("comfyui_ref")

    return result, updates
