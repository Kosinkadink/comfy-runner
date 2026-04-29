"""End-to-end PR-review preparation.

Orchestration layer that turns a ``(pr, repo, target)`` triple into a
fully-prepared environment: PR code checked out, dependencies
installed, manifest workflows on disk in ComfyUI's load directory,
and required models downloaded.

This module is target-aware but transport-agnostic — the same
``ReviewResult`` shape is returned no matter where ComfyUI ends up
running. v1 implements the local target (a comfy-runner installation
on the same machine); the remote and runpod targets reuse the
manifest helpers below and add their own deploy + provision
transports in subsequent PRs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import manifest as _manifest
from .workflow_models import (
    check_missing_models,
    download_models,
    resolve_models_dir,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ReviewResult:
    """Outcome of preparing one review target.

    The shape is deliberately uniform across target kinds so the
    rendering layer (CLI / dashboard) doesn't care whether the work
    happened locally or on a pod.

    ``failures`` accumulates non-fatal problems — a single 404 on one
    workflow URL or one model URL doesn't abort the whole prep — so
    the caller can report partial success and surface what to fix.
    """

    target_name: str
    install_path: str | None = None
    deploy: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None
    resolved: dict[str, Any] | None = None
    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    workflows_dir: str | None = None
    comfy_url: str | None = None

    def is_partial(self) -> bool:
        return bool(self.failures or self.failed)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "partial": self.is_partial(),
        }


# ---------------------------------------------------------------------------
# Workflow destination
# ---------------------------------------------------------------------------

def workflows_dest_for(install_path: str | Path) -> Path:
    """Return the directory where review workflow files should be saved.

    ComfyUI's UI scans ``user/default/workflows/`` for entries that
    appear in the load menu, so dropping fetched workflows there means
    the reviewer can pick them straight from the UI.
    """
    return Path(install_path) / "ComfyUI" / "user" / "default" / "workflows"


# ---------------------------------------------------------------------------
# Manifest fetch + resolve (shared across local / remote / runpod)
# ---------------------------------------------------------------------------

def fetch_and_resolve_manifest(
    owner: str,
    repo: str,
    pr: int,
    workflows_dest: Path,
    *,
    github_token: str | None = None,
    extra_models: list[_manifest.ModelEntry] | None = None,
    extra_workflows: list[str] | None = None,
    allow_arbitrary_urls: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> tuple[_manifest.Manifest | None, _manifest.ResolvedManifest | None]:
    """Fetch the PR body, parse the manifest block, fetch workflows.

    Returns ``(parsed_manifest, resolved)``.

    ``parsed_manifest`` is ``None`` when the PR has no
    ``comfyrunner`` block AND no extras were supplied — i.e. nothing
    to provision.

    ``resolved`` is ``None`` when the parsed manifest is empty (no
    models, no workflows). When non-None it contains the saved
    workflow paths plus the deduped model entries.

    Per-workflow fetch errors are collected on
    :attr:`ResolvedManifest.failures`; a malformed manifest block is
    surfaced via *send_output* but doesn't raise.
    """
    out = send_output or (lambda _t: None)

    parsed: _manifest.Manifest | None = None
    body = ""
    try:
        body = _manifest.fetch_pr_body(
            owner, repo, pr, github_token=github_token,
        )
    except RuntimeError as e:
        out(f"  ⚠ Could not fetch PR body: {e}\n")

    if body:
        try:
            parsed = _manifest.parse_manifest_block(body)
        except ValueError as e:
            out(
                f"  ⚠ PR has a comfyrunner block but it failed to parse: {e}\n"
            )
            parsed = None

    extra_models = extra_models or []
    extra_workflows = extra_workflows or []
    if parsed is None and not extra_models and not extra_workflows:
        return None, None
    if parsed is None:
        parsed = _manifest.Manifest()
    if extra_models:
        parsed.models.extend(extra_models)
    if extra_workflows:
        parsed.workflows.extend(extra_workflows)

    if parsed.is_empty():
        return parsed, None

    out(
        f"Resolving manifest "
        f"({len(parsed.models)} explicit model(s), "
        f"{len(parsed.workflows)} workflow URL(s))...\n"
    )

    resolved = _manifest.resolve(
        parsed, workflows_dest,
        allow_arbitrary_urls=allow_arbitrary_urls,
        send_output=out,
    )
    return parsed, resolved


# ---------------------------------------------------------------------------
# Model provisioning (local-install variant)
# ---------------------------------------------------------------------------

def provision_models_local(
    install_path: str | Path,
    models: list[_manifest.ModelEntry],
    *,
    token: str = "",
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Download missing models from *models* into the install's models dir.

    Returns the same shape as
    :func:`comfy_runner.workflow_models.download_models` plus a
    ``"skipped"`` list that includes entries already present before
    download even started.
    """
    out = send_output or (lambda _t: None)
    if not models:
        return {
            "downloaded": [], "skipped": [], "failed": [], "errors": [],
        }
    models_dir = resolve_models_dir(install_path)
    model_dicts = [m.to_dict() for m in models]
    missing, existing = check_missing_models(model_dicts, models_dir)
    pre_skipped = [f"{m['directory']}/{m['name']}" for m in existing]
    if not missing:
        out(f"All {len(model_dicts)} model(s) already present.\n")
        return {
            "downloaded": [], "skipped": pre_skipped,
            "failed": [], "errors": [],
        }
    out(f"Provisioning {len(missing)} missing model(s)...\n")
    result = download_models(
        missing, models_dir, send_output=out, token=token,
    )
    result["skipped"] = pre_skipped + list(result.get("skipped", []))
    return result


# ---------------------------------------------------------------------------
# Local target — full prep (called after deploy)
# ---------------------------------------------------------------------------

def prepare_local_review(
    install_path: str | Path,
    owner: str,
    repo: str,
    pr: int,
    *,
    github_token: str | None = None,
    download_token: str = "",
    extra_models: list[_manifest.ModelEntry] | None = None,
    extra_workflows: list[str] | None = None,
    allow_arbitrary_urls: bool = False,
    skip_provisioning: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run manifest fetch + workflow fetch + model provision against
    a local installation.

    Assumes the deploy step has already happened upstream — this
    function is the "after deploy" half of the review flow.
    """
    out = send_output or (lambda _t: None)
    workflows_dest = workflows_dest_for(install_path)

    parsed, resolved = fetch_and_resolve_manifest(
        owner, repo, pr, workflows_dest,
        github_token=github_token,
        extra_models=extra_models,
        extra_workflows=extra_workflows,
        allow_arbitrary_urls=allow_arbitrary_urls,
        send_output=out,
    )

    base = {
        "manifest": None,
        "resolved": None,
        "downloaded": [],
        "skipped": [],
        "failed": [],
        "errors": [],
        "workflows": [],
        "workflows_dir": str(workflows_dest),
        "failures": [],
    }

    if parsed is None:
        out("No manifest found in PR description; skipping provisioning.\n")
        return base

    base["manifest"] = {
        "models": [m.to_dict() for m in parsed.models],
        "workflows": list(parsed.workflows),
    }

    if resolved is None:
        return base

    base["resolved"] = resolved.to_dict()
    base["workflows"] = [str(p) for p in resolved.workflow_files]
    base["failures"] = list(resolved.failures)

    if skip_provisioning:
        out("Model provisioning skipped (--no-provision-models).\n")
        return base

    dl = provision_models_local(
        install_path, resolved.models,
        token=download_token,
        send_output=out,
    )
    base["downloaded"] = list(dl.get("downloaded", []))
    base["skipped"] = list(dl.get("skipped", []))
    base["failed"] = list(dl.get("failed", []))
    base["errors"] = list(dl.get("errors", []))
    return base


# ---------------------------------------------------------------------------
# Remote target — central-station-mediated review on an existing pod
# ---------------------------------------------------------------------------

def prepare_remote_review(
    server_url: str,
    pod_name: str,
    install_name: str,
    owner: str,
    repo: str,
    pr: int,
    *,
    github_token: str | None = None,
    download_token: str = "",
    extra_models: list[_manifest.ModelEntry] | None = None,
    extra_workflows: list[str] | None = None,
    allow_arbitrary_urls: bool = False,
    skip_provisioning: bool = False,
    force_purpose: bool = False,
    force_deploy: bool = False,
    idle_timeout_s: int | None = None,
    send_output: Callable[[str], None] | None = None,
    poll_timeout: int = 1800,
) -> dict[str, Any]:
    """Trigger a review against an existing pod via the central station.

    POSTs to ``{server_url}/pods/{pod_name}/review`` which auto-wakes the
    pod (if stopped), deploys the PR via its sidecar, and runs
    ``prepare_local_review`` server-side.

    Returns the same shape as :func:`prepare_local_review` (the
    ``review_result`` portion of the station's job result), augmented with
    ``pod_name``, ``server_url``, and ``deploy_result``.

    Raises ``RuntimeError`` on transport / job failure.
    """
    from .hosted.remote import RemoteRunner

    body: dict[str, Any] = {
        "install": install_name,
        "owner": owner,
        "repo": repo,
        "pr": int(pr),
        "allow_arbitrary_urls": bool(allow_arbitrary_urls),
        "skip_provisioning": bool(skip_provisioning),
    }
    if github_token:
        body["github_token"] = github_token
    if download_token:
        body["download_token"] = download_token
    if extra_models:
        body["extra_models"] = [m.to_dict() for m in extra_models]
    if extra_workflows:
        body["extra_workflows"] = list(extra_workflows)
    if force_purpose:
        body["force_purpose"] = True
    if force_deploy:
        body["force_deploy"] = True
    if idle_timeout_s is not None:
        body["idle_timeout_s"] = int(idle_timeout_s)

    runner = RemoteRunner(server_url)
    data = runner._request(
        "POST", f"/pods/{pod_name}/review", json=body,
    )
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError("Station did not return a job_id for review")

    final = runner.poll_job(
        job_id, timeout=poll_timeout, on_output=send_output,
    )

    review_result = dict(final.get("review_result") or {})
    review_result["pod_name"] = final.get("pod_name", pod_name)
    review_result["pod_purpose"] = final.get("pod_purpose")
    review_result["server_url"] = final.get("server_url", "")
    review_result["deploy_result"] = final.get("deploy_result")
    if final.get("idle_timeout_s") is not None:
        review_result["idle_timeout_s"] = final.get("idle_timeout_s")
    return review_result


# ---------------------------------------------------------------------------
# Runpod target — ephemeral fresh pod per PR via the central station
# ---------------------------------------------------------------------------

def prepare_runpod_review(
    server_url: str,
    owner: str,
    repo: str,
    pr: int,
    *,
    install_name: str = "main",
    gpu_type: str | None = None,
    idle_timeout_s: int | None = None,
    image: str | None = None,
    volume_id: str | None = None,
    volume_size_gb: int | None = None,
    datacenter: str | None = None,
    cloud_type: str | None = None,
    github_token: str | None = None,
    download_token: str = "",
    extra_models: list[_manifest.ModelEntry] | None = None,
    extra_workflows: list[str] | None = None,
    allow_arbitrary_urls: bool = False,
    skip_provisioning: bool = False,
    send_output: Callable[[str], None] | None = None,
    poll_timeout: int = 1800,
) -> dict[str, Any]:
    """Provision (or wake) an ephemeral PR pod and prepare it for review.

    Two-step orchestration against the central station:

    1. ``POST /pods/launch-pr`` creates-or-wakes a pod tagged
       ``purpose='pr'`` / ``pr_number=<pr>`` and deploys the PR.
    2. ``POST /pods/{name}/review`` with ``skip_deploy=True`` runs the
       review-prep step (manifest + workflows + models).

    The pod stays alive after this call returns; the central server's
    idle reaper auto-stops it after ``idle_timeout_s`` of inactivity.
    Use :func:`cleanup_runpod_review` to terminate it sooner.

    Returns the same ``ReviewResult`` dict shape as
    :func:`prepare_remote_review`, with extra ``created_new`` and
    ``idle_timeout_s`` fields from the launch step.
    """
    from .hosted.remote import RemoteRunner

    out = send_output or (lambda _t: None)

    # ── 1) Launch-or-wake the PR pod (deploys as part of the call). ──
    launch_body: dict[str, Any] = {
        "pr": int(pr),
        "repo": f"https://github.com/{owner}/{repo}",
        "install": install_name,
    }
    if gpu_type:
        launch_body["gpu_type"] = gpu_type
    if idle_timeout_s is not None:
        launch_body["idle_timeout_s"] = int(idle_timeout_s)
    if image:
        launch_body["image"] = image
    if volume_id:
        launch_body["volume_id"] = volume_id
    if volume_size_gb is not None:
        launch_body["volume_size_gb"] = int(volume_size_gb)
    if datacenter:
        launch_body["datacenter"] = datacenter
    if cloud_type:
        launch_body["cloud_type"] = cloud_type

    runner = RemoteRunner(server_url)
    out("Provisioning PR pod via central station...\n")
    launch_resp = runner._request(
        "POST", "/pods/launch-pr", json=launch_body,
    )
    launch_job_id = launch_resp.get("job_id")
    if not launch_job_id:
        raise RuntimeError("Station did not return a job_id for launch-pr")
    pod_name = launch_resp.get("name") or ""

    launch_result = runner.poll_job(
        launch_job_id, timeout=poll_timeout, on_output=send_output,
    )
    pod_name = launch_result.get("name") or pod_name
    if not pod_name:
        raise RuntimeError("launch-pr did not return a pod name")

    # ── 2) Run the review-prep step (skip_deploy — already deployed). ─
    review_body: dict[str, Any] = {
        "install": install_name,
        "owner": owner,
        "repo": repo,
        "pr": int(pr),
        "skip_deploy": True,
        "allow_arbitrary_urls": bool(allow_arbitrary_urls),
        "skip_provisioning": bool(skip_provisioning),
    }
    if github_token:
        review_body["github_token"] = github_token
    if download_token:
        review_body["download_token"] = download_token
    if extra_models:
        review_body["extra_models"] = [m.to_dict() for m in extra_models]
    if extra_workflows:
        review_body["extra_workflows"] = list(extra_workflows)

    review_resp = runner._request(
        "POST", f"/pods/{pod_name}/review", json=review_body,
    )
    review_job_id = review_resp.get("job_id")
    if not review_job_id:
        raise RuntimeError("Station did not return a job_id for review")

    final = runner.poll_job(
        review_job_id, timeout=poll_timeout, on_output=send_output,
    )

    review_result = dict(final.get("review_result") or {})
    review_result["pod_name"] = final.get("pod_name", pod_name)
    review_result["pod_purpose"] = final.get("pod_purpose")
    review_result["server_url"] = (
        final.get("server_url") or launch_result.get("server_url") or ""
    )
    review_result["deploy_result"] = launch_result.get("deploy_result")
    review_result["created_new"] = bool(launch_result.get("created", False))
    review_result["idle_timeout_s"] = launch_result.get("idle_timeout_s")
    return review_result


def cleanup_runpod_review(
    server_url: str,
    pr: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Terminate ephemeral PR pods (``purpose='pr'``) for a given PR.

    Wraps ``POST /reviews/cleanup`` on the central station. Returns the
    server's response dict (``terminated``, ``skipped``, ``total_found``,
    …).
    """
    from .hosted.remote import RemoteRunner

    runner = RemoteRunner(server_url)
    return runner._request(
        "POST", "/reviews/cleanup",
        json={"pr": int(pr), "dry_run": bool(dry_run)},
    )
