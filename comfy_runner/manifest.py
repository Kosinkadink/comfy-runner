"""PR-review manifest fetching and resolution.

A *manifest* is a small JSON document declaring what's needed to run a
PR's demo workflows: model URLs and workflow URLs. The standard place
for it is a fenced ``comfyrunner`` block inside the PR's GitHub
description; programmatically a manifest can also be supplied as a
plain dict (e.g. via CLI flags or test fixtures).

This module provides:

* :func:`fetch_pr_body` — pull a PR body via GitHub's REST API.
* :func:`parse_manifest_block` — extract the fenced block and validate
  it into a :class:`Manifest`.
* :func:`fetch_workflow` — HTTPS-GET a workflow URL with size & host
  checks, save the JSON to a destination directory.
* :func:`resolve` — drive the workflow fetches, extract embedded
  ``node.properties.models`` declarations from each, dedupe with the
  manifest's explicit ``models`` list, and return a
  :class:`ResolvedManifest` ready to feed
  :func:`comfy_runner.workflow_models.download_models`.

The module is transport-agnostic: the same code is called whether
review prep happens locally (``comfy_runner.py review``) or remotely
(central server proxying to a pod's sidecar).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .workflow_models import parse_workflow_models


# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

# Hosts allowed for workflow / model URL fetches by default. Operators
# can extend this list via ``manifest_url_allowlist`` in the central
# server's hosted config.
DEFAULT_URL_ALLOWLIST: tuple[str, ...] = (
    "huggingface.co",
    "civitai.com",
    "modelscope.cn",
    "gist.githubusercontent.com",
    "raw.githubusercontent.com",
    "github.com",
)

# Maximum size of a fetched workflow JSON file (10 MB). Real workflows
# are well under 100 KB; this is a sanity cap to fail fast on a
# misconfigured URL serving binary garbage.
MAX_WORKFLOW_BYTES = 10 * 1024 * 1024

# Maximum size of a PR body that we'll scan for the manifest block.
# GitHub's hard cap is ~65 KB; we accept up to 256 KB to be defensive.
MAX_PR_BODY_BYTES = 256 * 1024

# Regex for the manifest block. Accepts ```comfyrunner or
# ```comfy-runner as the language tag (both spellings have appeared in
# discussion). Anchored to start-of-line so we don't accidentally match
# language tags embedded in other code blocks.
_BLOCK_RE = re.compile(
    r"^```(?:comfyrunner|comfy-runner)[^\n]*\n(.*?)\n```",
    re.MULTILINE | re.DOTALL,
)

_GITHUB_API = "https://api.github.com"
_GITHUB_TIMEOUT = 30
_WORKFLOW_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelEntry:
    """A single model declaration: ``name`` (filename), ``url`` (HTTPS
    source), ``directory`` (subdirectory under ComfyUI's ``models/``)."""

    name: str
    url: str
    directory: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "url": self.url, "directory": self.directory}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelEntry":
        name = d.get("name", "")
        url = d.get("url", "")
        directory = d.get("directory", "")
        if not (isinstance(name, str) and name):
            raise ValueError(f"model entry missing 'name': {d!r}")
        if not (isinstance(url, str) and url):
            raise ValueError(f"model entry missing 'url': {d!r}")
        if not (isinstance(directory, str) and directory):
            raise ValueError(f"model entry missing 'directory': {d!r}")
        return cls(name=name, url=url, directory=directory)


@dataclass
class Manifest:
    """A parsed manifest, post-validation but pre-fetch."""

    models: list[ModelEntry] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.models and not self.workflows


@dataclass
class ResolvedManifest:
    """The end-product of manifest resolution: ready to provision.

    ``models`` is the deduplicated union of the manifest's explicit
    ``models`` list and the ``node.properties.models`` entries pulled
    from every successfully-fetched workflow. ``workflow_files`` is the
    list of paths workflows were saved to (one per successful fetch).
    ``failures`` collects per-workflow fetch errors so the caller can
    surface a partial-success report without raising.
    """

    models: list[ModelEntry]
    workflow_files: list[Path]
    failures: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": [m.to_dict() for m in self.models],
            "workflows": [str(p) for p in self.workflow_files],
            "failures": list(self.failures),
        }


# ---------------------------------------------------------------------------
# Block extraction & validation
# ---------------------------------------------------------------------------

def parse_manifest_block(body_text: str) -> Manifest | None:
    """Find and parse the first ``comfyrunner`` fenced block in *body_text*.

    Returns ``None`` if no block is present.

    Raises ``ValueError`` on malformed JSON or invalid schema so authors
    see a clear error rather than a silent skip.
    """
    if not body_text:
        return None
    if len(body_text) > MAX_PR_BODY_BYTES:
        raise ValueError(
            f"PR body is suspiciously large ({len(body_text)} bytes); "
            "refusing to scan for manifest"
        )
    match = _BLOCK_RE.search(body_text)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        raise ValueError("comfyrunner block is empty")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"comfyrunner block is not valid JSON: {e.msg} (line {e.lineno})"
        ) from e
    return validate_manifest(data)


def validate_manifest(data: Any) -> Manifest:
    """Validate a parsed manifest dict and return a typed :class:`Manifest`.

    Raises ``ValueError`` on schema violations.
    """
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")

    raw_models = data.get("models") or []
    if not isinstance(raw_models, list):
        raise ValueError("'models' must be a list")
    models: list[ModelEntry] = []
    for i, m in enumerate(raw_models):
        if not isinstance(m, dict):
            raise ValueError(
                f"models[{i}] must be an object, got {type(m).__name__}"
            )
        models.append(ModelEntry.from_dict(m))

    raw_workflows = data.get("workflows") or []
    if not isinstance(raw_workflows, list):
        raise ValueError("'workflows' must be a list")
    workflows: list[str] = []
    for i, w in enumerate(raw_workflows):
        if not isinstance(w, str) or not w:
            raise ValueError(
                f"workflows[{i}] must be a non-empty URL string"
            )
        workflows.append(w)

    return Manifest(models=models, workflows=workflows)


# ---------------------------------------------------------------------------
# GitHub fetching
# ---------------------------------------------------------------------------

def fetch_pr_body(
    owner: str,
    repo: str,
    pr: int,
    github_token: str | None = None,
) -> str:
    """Return the body text of GitHub PR ``owner/repo#pr``.

    Uses *github_token* if given, otherwise falls back to the
    ``GITHUB_TOKEN`` environment variable. Public repos work
    unauthenticated but are subject to GitHub's anonymous rate limits.

    Raises ``RuntimeError`` on network errors or non-200 responses
    (with a clearer message for 404).
    """
    if not owner or not repo:
        raise ValueError("owner and repo are required")
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError("pr must be a positive integer")

    token = github_token or os.environ.get("GITHUB_TOKEN") or ""
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "comfy-runner",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/pulls/{pr}"
    try:
        resp = requests.get(url, headers=headers, timeout=_GITHUB_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"failed to fetch PR #{pr}: {e}") from e
    if resp.status_code == 404:
        raise RuntimeError(
            f"PR not found: {owner}/{repo}#{pr} "
            "(or the repo is private and no token was provided)"
        )
    if (
        resp.status_code == 403
        and resp.headers.get("X-RateLimit-Remaining") == "0"
    ):
        reset = resp.headers.get("X-RateLimit-Reset", "")
        raise RuntimeError(
            f"GitHub rate limit exceeded for {owner}/{repo}#{pr}"
            + (f" (resets at {reset})" if reset else "")
            + "; set GITHUB_TOKEN to authenticate (much higher limits) "
            "or wait for the reset window"
        )
    if resp.status_code == 401 or resp.status_code == 403:
        raise RuntimeError(
            f"GitHub authentication failed for {owner}/{repo}#{pr} "
            f"(status {resp.status_code}); set GITHUB_TOKEN or pass --token"
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API returned {resp.status_code} for {owner}/{repo}#{pr}: "
            f"{resp.text[:200]}"
        )
    body = resp.json().get("body") or ""
    if not isinstance(body, str):
        raise RuntimeError(
            f"GitHub API returned non-string body for {owner}/{repo}#{pr}"
        )
    return body


# ---------------------------------------------------------------------------
# URL allowlist check
# ---------------------------------------------------------------------------

def is_url_allowed(
    url: str,
    allowlist: tuple[str, ...] = DEFAULT_URL_ALLOWLIST,
) -> bool:
    """Check that *url* is HTTPS and its host matches *allowlist*.

    Matches a host either exactly or as a subdomain (so
    ``cdn.huggingface.co`` matches ``huggingface.co``).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in allowlist)


# ---------------------------------------------------------------------------
# Workflow fetching
# ---------------------------------------------------------------------------

def fetch_workflow(
    url: str,
    dest_dir: Path,
    *,
    allowlist: tuple[str, ...] = DEFAULT_URL_ALLOWLIST,
    allow_arbitrary_urls: bool = False,
    filename_override: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Fetch *url*, save into *dest_dir*, and return ``(path, parsed_json)``.

    The filename is derived from the URL's last path segment and run
    through :func:`safe_file.is_safe_path_component`. Refuses to write
    outside *dest_dir*.

    Raises ``ValueError`` (host not allowed, unsafe filename) or
    ``RuntimeError`` (HTTP error, oversized response, malformed JSON,
    not a workflow shape).
    """
    from safe_file import is_safe_path_component  # local import; module lives at repo root

    if not allow_arbitrary_urls and not is_url_allowed(url, allowlist):
        raise ValueError(
            f"workflow URL host not in allowlist: {url} "
            f"(allowed: {', '.join(allowlist)})"
        )

    parsed = urlparse(url)
    if filename_override is not None:
        candidate = filename_override
    else:
        candidate = (parsed.path or "").rsplit("/", 1)[-1]
        if "?" in candidate:
            candidate = candidate.split("?", 1)[0]
    candidate = candidate.strip()
    if not candidate:
        candidate = "workflow.json"
    if not candidate.lower().endswith(".json"):
        candidate = candidate + ".json"
    if not is_safe_path_component(candidate):
        raise ValueError(
            f"unsafe workflow filename derived from URL: {candidate!r}"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / candidate
    if not dest_path.resolve().is_relative_to(dest_dir.resolve()):
        raise ValueError(f"workflow path escapes destination: {dest_path}")

    try:
        resp = requests.get(url, timeout=_WORKFLOW_TIMEOUT, stream=True)
    except requests.RequestException as e:
        raise RuntimeError(f"failed to fetch workflow {url}: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(
            f"workflow URL returned HTTP {resp.status_code}: {url}"
        )

    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_WORKFLOW_BYTES:
            raise RuntimeError(
                f"workflow at {url} exceeds {MAX_WORKFLOW_BYTES} bytes"
            )
        chunks.append(chunk)

    raw = b"".join(chunks)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"workflow at {url} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"workflow at {url} is not a JSON object")
    if not _looks_like_workflow(data):
        raise RuntimeError(
            f"workflow at {url} doesn't look like a ComfyUI workflow "
            "(no 'nodes' field and not in API format)"
        )

    dest_path.write_bytes(raw)
    return dest_path, data


def _looks_like_workflow(data: dict[str, Any]) -> bool:
    """Return True if *data* resembles a ComfyUI workflow.

    Accepts both the editor format (``{"nodes": [...]}``) and the API
    format (``{"1": {"class_type": ...}, "2": {...}}``).
    """
    if isinstance(data.get("nodes"), list):
        return True
    if not data:
        return False
    # API format: every key is a numeric string and every value has
    # ``class_type``.
    return all(
        isinstance(k, str)
        and k.isdigit()
        and isinstance(v, dict)
        and "class_type" in v
        for k, v in data.items()
    )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(
    manifest: Manifest,
    workflows_dest: Path,
    *,
    allowlist: tuple[str, ...] = DEFAULT_URL_ALLOWLIST,
    allow_arbitrary_urls: bool = False,
    send_output: Callable[[str], None] | None = None,
) -> ResolvedManifest:
    """Fetch all workflow URLs in *manifest*, extract embedded model
    declarations, and return a deduplicated :class:`ResolvedManifest`.

    Per-workflow fetch errors are recorded in
    :attr:`ResolvedManifest.failures` rather than raised, so a single
    bad URL doesn't abort an otherwise-good provisioning run.
    """
    out = send_output or (lambda _t: None)

    workflow_paths: list[Path] = []
    embedded: list[ModelEntry] = []
    failures: list[dict[str, str]] = []

    for url in manifest.workflows:
        try:
            path, data = fetch_workflow(
                url, workflows_dest,
                allowlist=allowlist,
                allow_arbitrary_urls=allow_arbitrary_urls,
            )
        except (ValueError, RuntimeError) as e:
            failures.append({"url": url, "error": str(e)})
            out(f"  ✗ {url}: {e}\n")
            continue
        workflow_paths.append(path)
        size_kb = path.stat().st_size / 1024
        out(f"  ✓ {path.name} ({size_kb:.1f} KB)\n")
        for raw in parse_workflow_models(data):
            try:
                embedded.append(ModelEntry.from_dict(raw))
            except ValueError:
                # Workflow had a malformed embedded entry; skip silently —
                # this isn't the manifest author's fault.
                continue

    # Dedupe with explicit manifest models taking priority over embedded
    # models on key collision (so an explicit URL wins over an old URL
    # baked into a workflow).
    seen: set[tuple[str, str]] = set()
    deduped: list[ModelEntry] = []
    for m in list(manifest.models) + embedded:
        key = (m.directory, m.name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)

    return ResolvedManifest(
        models=deduped,
        workflow_files=workflow_paths,
        failures=failures,
    )
