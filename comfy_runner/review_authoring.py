"""Manifest authoring tools for PR-review.

Two pure-local helpers used by the ``review-init`` and ``review-validate``
CLI commands. Neither hits the central station; both are safe to run on
a developer's laptop.

* :func:`generate_block` reads a workflow JSON file, pulls
  ``node.properties.models`` declarations out of it, and emits a fenced
  ``comfyrunner`` block ready to paste into a PR description.
* :func:`lint_manifest_source` accepts a path, an ``owner/repo#pr``
  shorthand, or a GitHub PR URL, finds the manifest payload, and
  reports schema problems.

The goal is to give PR authors a fast feedback loop *before* anyone
runs ``review --target ...`` against their PR, so missing-manifest and
malformed-manifest issues are caught at the source rather than papered
over by an out-of-band override.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .manifest import (
    DEFAULT_URL_ALLOWLIST,
    MAX_PR_BODY_BYTES,
    Manifest,
    ModelEntry,
    fetch_pr_body,
    is_url_allowed,
    parse_manifest_block,
    validate_manifest,
)
from .workflow_models import parse_workflow_models


# ---------------------------------------------------------------------------
# Block generation (review-init)
# ---------------------------------------------------------------------------

_BLOCK_FENCE = "```"

# Placeholder strings emitted into generated blocks when the user did
# not provide a value. The author is expected to swap these out before
# pasting into a PR description.
PLACEHOLDER_WORKFLOW_URL = "FILL_ME_IN_workflow_url"
PLACEHOLDER_MODEL_URL = "FILL_ME_IN_model_url"


@dataclass(frozen=True)
class GeneratedBlock:
    """The result of :func:`generate_block`.

    ``text`` is the full fenced block (suitable for direct print);
    ``manifest_dict`` is the inner JSON object so callers can re-emit
    it in another shape (e.g. ``--json`` mode). ``warnings`` lists
    advisory notes about missing data the author should fill in.
    """

    text: str
    manifest_dict: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _load_workflow_json(workflow_path: Path) -> dict[str, Any]:
    try:
        raw = workflow_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"workflow file is not valid UTF-8 text: {e}"
        ) from e
    except OSError as e:
        raise ValueError(
            f"could not read workflow file {workflow_path}: {e}"
        ) from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"workflow file is not valid JSON: {e.msg} (line {e.lineno})"
        ) from e
    if not isinstance(data, dict):
        raise ValueError(
            "workflow file must contain a JSON object at the top level"
        )
    return data


def generate_block(
    workflow_path: Path,
    workflow_url: str | None = None,
) -> GeneratedBlock:
    """Read *workflow_path* and emit a ``comfyrunner`` block for it.

    *workflow_url* is the public HTTPS URL where the workflow file will
    live (typically a ``raw.githubusercontent.com`` URL pointing at the
    PR's branch). When omitted, a placeholder is emitted and a warning
    added so the author knows to fill it in.

    Models are pulled from each node's ``properties.models`` list using
    :func:`parse_workflow_models`; entries missing any of
    ``name``/``url``/``directory`` are skipped (the underlying parser
    enforces this).

    Raises ``ValueError`` if the file is unreadable, not JSON, or not a
    JSON object.
    """
    if not workflow_path.is_file():
        raise ValueError(f"workflow file not found: {workflow_path}")

    workflow = _load_workflow_json(workflow_path)
    models = parse_workflow_models(workflow)

    warnings: list[str] = []
    final_url = (workflow_url or "").strip()
    if not final_url:
        final_url = PLACEHOLDER_WORKFLOW_URL
        warnings.append(
            "No --workflow-url given; substitute the placeholder before "
            "pasting (typically a raw.githubusercontent.com URL pointing "
            "at the workflow file in your PR's branch)."
        )

    if not models:
        warnings.append(
            "No models found in workflow nodes' properties.models. If "
            "this PR needs models, add them by hand to the manifest's "
            "'models' list."
        )

    manifest_dict: dict[str, Any] = {
        "workflows": [final_url],
        "models": models,
    }

    inner = json.dumps(manifest_dict, indent=2)
    text = f"{_BLOCK_FENCE}comfyrunner\n{inner}\n{_BLOCK_FENCE}"

    return GeneratedBlock(
        text=text, manifest_dict=manifest_dict, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Lint result types (review-validate)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LintFinding:
    """A single lint result.

    *severity* is ``"error"`` (parse / schema problem; manifest is
    unusable), ``"warn"`` (suspect but not strictly invalid), or
    ``"info"`` (advisory). *path* is a JSON-pointer-ish hint like
    ``"models[1].url"`` when applicable.
    """

    severity: str
    message: str
    path: str = ""


@dataclass
class LintResult:
    """The output of :func:`lint_manifest_source`."""

    source: str
    found_block: bool
    manifest: Manifest | None
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` when the manifest parsed and has no error findings."""
        return self.found_block and not any(
            f.severity == "error" for f in self.findings
        )


# ---------------------------------------------------------------------------
# Source resolution (review-validate)
# ---------------------------------------------------------------------------

# ``owner/repo#pr`` shorthand. Owner and repo allow the usual GitHub
# character set (alphanum, ``-``, ``_``, ``.``). PR is a positive int.
_SHORTHAND_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9._-]+)#(\d+)$"
)
_PR_URL_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9._-]+)/pull/"
    r"(?P<pr>\d+)(?:[/?#].*)?$"
)


@dataclass(frozen=True)
class _ResolvedSource:
    label: str
    text: str


def _resolve_source(
    source: str,
    *,
    github_token: str | None = None,
) -> _ResolvedSource:
    """Resolve *source* to a (label, text) pair.

    Accepts (in priority order):

    * ``owner/repo#pr`` — fetch PR body via GitHub API.
    * ``https://github.com/<owner>/<repo>/pull/<pr>`` — same.
    * Otherwise treat as a local file path and read it. Files may be
      either a raw manifest JSON or a Markdown blob containing a
      fenced ``comfyrunner`` block.

    Raises ``ValueError`` for empty input, ``RuntimeError`` for
    network / file errors.
    """
    s = (source or "").strip()
    if not s:
        raise ValueError("source must not be empty")

    url_match = _PR_URL_RE.match(s)
    if url_match:
        owner = url_match.group("owner")
        repo = url_match.group("repo")
        pr = int(url_match.group("pr"))
        body = fetch_pr_body(owner, repo, pr, github_token=github_token)
        return _ResolvedSource(
            label=f"{owner}/{repo}#{pr} (GitHub PR body)",
            text=body,
        )

    short_match = _SHORTHAND_RE.match(s)
    if short_match:
        owner, repo, pr_str = short_match.groups()
        pr = int(pr_str)
        body = fetch_pr_body(owner, repo, pr, github_token=github_token)
        return _ResolvedSource(
            label=f"{owner}/{repo}#{pr} (GitHub PR body)",
            text=body,
        )

    path = Path(s)
    if not path.is_file():
        raise RuntimeError(f"file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(
            f"file is not valid UTF-8 text: {path} ({e})"
        ) from e
    except OSError as e:
        raise RuntimeError(f"could not read file {path}: {e}") from e
    return _ResolvedSource(label=str(path), text=text)


# ---------------------------------------------------------------------------
# Lint logic
# ---------------------------------------------------------------------------

def _lint_manifest_object(manifest: Manifest) -> list[LintFinding]:
    """Run extra checks on an already-validated :class:`Manifest`.

    These are checks the schema validator does not enforce — e.g. URL
    scheme and the default-allowlist host check — so authors get a
    warning before their PR fails review prep.
    """
    # Imported locally because ``safe_file`` lives at the comfy-runner
    # repo root, not inside the package — see the same pattern in
    # ``comfy_runner/manifest.py``.
    from safe_file import is_safe_path_component

    findings: list[LintFinding] = []
    for i, m in enumerate(manifest.models):
        path_models = f"models[{i}]"
        if not _is_https(m.url):
            findings.append(LintFinding(
                "error",
                f"model URL is not HTTPS: {m.url!r}",
                f"{path_models}.url",
            ))
        elif not is_url_allowed(m.url, DEFAULT_URL_ALLOWLIST):
            findings.append(LintFinding(
                "warn",
                (
                    f"model URL host is not in the default allowlist "
                    f"({', '.join(DEFAULT_URL_ALLOWLIST)}); pod operator "
                    f"will need to pass --allow-arbitrary-urls"
                ),
                f"{path_models}.url",
            ))
        # Names must be a single safe filename component (no separators,
        # no '.' / '..'). Use the project's canonical
        # is_safe_path_component check rather than ad-hoc string slicing.
        if not is_safe_path_component(m.name):
            findings.append(LintFinding(
                "error",
                f"model name must be a bare filename, got {m.name!r}",
                f"{path_models}.name",
            ))
        # Directory must be relative and traversal-free. Reject absolute
        # paths (including Windows drive letters) and any component
        # that is not safe (covers '..', empty segments, separators
        # smuggled in within a single segment).
        dir_path = Path(m.directory)
        if dir_path.is_absolute() or any(
            not is_safe_path_component(part) for part in dir_path.parts
        ):
            findings.append(LintFinding(
                "error",
                f"model directory must be a relative path with no "
                f"traversal segments, got {m.directory!r}",
                f"{path_models}.directory",
            ))

    for i, w in enumerate(manifest.workflows):
        path_w = f"workflows[{i}]"
        if not _is_https(w):
            findings.append(LintFinding(
                "error",
                f"workflow URL is not HTTPS: {w!r}",
                path_w,
            ))
        elif not is_url_allowed(w, DEFAULT_URL_ALLOWLIST):
            findings.append(LintFinding(
                "warn",
                (
                    f"workflow URL host is not in the default allowlist "
                    f"({', '.join(DEFAULT_URL_ALLOWLIST)}); pod operator "
                    f"will need to pass --allow-arbitrary-urls"
                ),
                path_w,
            ))

    if not manifest.workflows and not manifest.models:
        findings.append(LintFinding(
            "warn",
            "manifest is empty (no workflows and no models)",
        ))

    return findings


def _is_https(url: str) -> bool:
    try:
        return urlparse(url).scheme == "https"
    except Exception:
        return False


def lint_manifest_text(text: str) -> tuple[bool, Manifest | None, list[LintFinding]]:
    """Lint a string that may contain a fenced ``comfyrunner`` block.

    Returns ``(found_block, manifest, findings)``.

    Resolution:

    1. If the input exceeds :data:`MAX_PR_BODY_BYTES`, refuse to scan
       and return ``found_block=False`` with an error finding (we
       haven't located a block — we just refused to look).
    2. Try :func:`parse_manifest_block`. If a block is present, run
       extra schema/URL checks; if present but malformed, return
       ``found_block=True`` with the parse error.
    3. Otherwise return ``found_block=False`` with an info finding.

    Free-form text that happens to start with ``{`` is *not* treated as
    a raw manifest body — that path is reserved for explicitly-typed
    sources (e.g. ``.json`` files) handled by
    :func:`lint_manifest_source`. This function never raises.
    """
    findings: list[LintFinding] = []

    # 1) Size precheck, separate from parse_manifest_block, so we can
    #    correctly report ``found_block=False`` (we never even scanned
    #    for a block) instead of misleadingly reporting that a block
    #    was found-but-invalid.
    if len(text) > MAX_PR_BODY_BYTES:
        findings.append(LintFinding(
            "error",
            f"source is too large to scan ({len(text)} bytes > "
            f"{MAX_PR_BODY_BYTES}); refusing to look for a manifest block",
        ))
        return False, None, findings

    # 2) Look for the fenced block.
    try:
        manifest = parse_manifest_block(text)
    except ValueError as e:
        findings.append(LintFinding("error", str(e)))
        return True, None, findings

    if manifest is not None:
        findings.extend(_lint_manifest_object(manifest))
        return True, manifest, findings

    # 3) No block.
    findings.append(LintFinding(
        "info",
        "no comfyrunner block found in this source",
    ))
    return False, None, findings


def lint_manifest_json(text: str) -> tuple[bool, Manifest | None, list[LintFinding]]:
    """Lint a string that is intended to be a raw manifest JSON object.

    Used by :func:`lint_manifest_source` when the source is explicitly
    typed as JSON (e.g. a ``.json`` file). Unlike
    :func:`lint_manifest_text`, this function does *not* fall back to
    "no block found" — invalid JSON or schema is always an error.

    Returns ``(found_block, manifest, findings)`` with the same shape
    as :func:`lint_manifest_text` for caller compatibility;
    ``found_block`` is ``True`` whenever the input was a JSON object
    we attempted to validate.
    """
    findings: list[LintFinding] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        findings.append(LintFinding(
            "error",
            f"manifest JSON is malformed: {e.msg} (line {e.lineno})",
        ))
        return True, None, findings
    try:
        manifest = validate_manifest(data)
    except ValueError as e:
        findings.append(LintFinding("error", str(e)))
        return True, None, findings
    findings.extend(_lint_manifest_object(manifest))
    return True, manifest, findings


def lint_manifest_source(
    source: str,
    *,
    github_token: str | None = None,
) -> LintResult:
    """Lint *source* (file path, ``owner/repo#pr``, or PR URL).

    File sources ending in ``.json`` are treated as raw manifest JSON
    via :func:`lint_manifest_json`; all other sources (markdown files,
    PR bodies fetched from GitHub) are scanned for a fenced
    ``comfyrunner`` block via :func:`lint_manifest_text`.

    Network and file errors are surfaced as a single ``error`` finding
    with ``found_block=False``; the function never raises.
    """
    try:
        resolved = _resolve_source(source, github_token=github_token)
    except (ValueError, RuntimeError) as e:
        return LintResult(
            source=source,
            found_block=False,
            manifest=None,
            findings=[LintFinding("error", str(e))],
        )

    # Pick the parsing strategy based on what kind of source this was.
    # We only treat raw-JSON-by-default for *local files* with a .json
    # extension. PR bodies (and anything else) go through the fenced-
    # block scanner so a free-form description that happens to start
    # with ``{`` is never mistaken for a malformed manifest.
    use_raw_json = (
        not resolved.label.endswith(" (GitHub PR body)")
        and Path(source.strip()).suffix.lower() == ".json"
    )
    if use_raw_json:
        found_block, manifest, findings = lint_manifest_json(resolved.text)
    else:
        found_block, manifest, findings = lint_manifest_text(resolved.text)

    return LintResult(
        source=resolved.label,
        found_block=found_block,
        manifest=manifest,
        findings=findings,
    )


__all__ = [
    "GeneratedBlock",
    "LintFinding",
    "LintResult",
    "PLACEHOLDER_MODEL_URL",
    "PLACEHOLDER_WORKFLOW_URL",
    "generate_block",
    "lint_manifest_json",
    "lint_manifest_source",
    "lint_manifest_text",
]
