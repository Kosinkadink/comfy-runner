"""Reporting — shared data model and multi-format renderers.

All renderers consume the same ``SuiteReport`` data model.  Data
assembly (from ``SuiteRun`` + comparison results) happens once in
``build_report()``, then each renderer is a thin formatting pass.

Supported formats:
- JSON   — machine-readable, for CI pipelines
- Console — Rich terminal output with colors
- HTML   — self-contained single-file with image diffs
- Markdown — GitHub-friendly tables for PR comments
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compare.registry import CompareResult
from .runner import ComparisonEntry, SuiteRun


# ===================================================================
# Shared data model
# ===================================================================

@dataclass
class WorkflowReport:
    """Report data for one workflow execution."""

    name: str
    passed: bool
    error: str | None = None
    execution_time: float | None = None
    output_count: int = 0
    has_baseline: bool = False
    comparisons: list[ComparisonEntry] = field(default_factory=list)
    # Bare filenames (no subfolder) of the workflow's output artifacts,
    # in the order ComfyUI returned them. Used by ``render_html`` to
    # show output thumbnails even when there is no baseline to compare
    # against — without this the HTML report has no visual content for
    # baseline-less runs and the operator has to dig through the file
    # tree to eyeball the outputs.
    outputs: list[str] = field(default_factory=list)

    @property
    def comparison_passed(self) -> bool:
        """True if all comparisons passed (or none were run)."""
        if not self.comparisons:
            return True
        return all(c.result.passed for c in self.comparisons)


@dataclass
class SuiteReport:
    """Complete report for a test suite run, ready for rendering."""

    suite_name: str
    timestamp: str
    duration: float
    total: int
    passed: int
    failed: int
    workflows: list[WorkflowReport] = field(default_factory=list)
    target_info: dict[str, Any] = field(default_factory=dict)
    # Suite-level watchdog metadata. ``timed_out`` is set when the
    # ``max_runtime_s`` budget was exceeded; ``aborted_reason`` carries
    # a short reason string (e.g. ``"overrun"``).
    timed_out: bool = False
    aborted_reason: str | None = None
    # Path to the suite directory on the host that produced this run.
    # Used by ``write_report`` to copy baseline files into the output
    # dir so the HTML report can render a baseline | test | diff
    # three-up tile per image comparison.  Stored as a string for
    # JSON-serializability.
    suite_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        d = asdict(self)
        # Convert CompareResult.diff_artifact Path → str
        for wf in d.get("workflows", []):
            for comp in wf.get("comparisons", []):
                res = comp.get("result", {})
                if res.get("diff_artifact") is not None:
                    res["diff_artifact"] = str(res["diff_artifact"])
        return d


# ===================================================================
# Data assembly
# ===================================================================

def build_report(
    suite_run: SuiteRun,
    comparisons: dict[str, list[ComparisonEntry]] | None = None,
    target_info: dict[str, Any] | None = None,
    suite_path: Path | str | None = None,
) -> SuiteReport:
    """Build a ``SuiteReport`` from a ``SuiteRun`` and optional comparisons.

    Args:
        suite_run: Completed test run.
        comparisons: Map of workflow_name → list of ComparisonEntry.
            If None, uses ``suite_run.comparisons`` (populated by
            ``run_suite()`` when baselines are present).
        target_info: Optional metadata about the test target.
        suite_path: Optional path to the suite directory.  Defaults to
            ``suite_run.suite_path``.  Recorded on the report so
            ``write_report`` can copy baseline files into the output
            dir for the HTML three-up tile.
    """
    if comparisons is None:
        comparisons = suite_run.comparisons
    workflows: list[WorkflowReport] = []

    for r in suite_run.results:
        wf_comparisons = comparisons.get(r.workflow_name, [])
        output_count = (
            sum(len(files) for files in r.prompt_result.outputs.values())
            if r.prompt_result
            else 0
        )
        exec_time = (
            r.prompt_result.execution_time
            if r.prompt_result and r.prompt_result.execution_time
            else None
        )
        # Flatten output filenames in node-result order. Bare names only
        # (no subfolder) because the run's output_dir already keeps
        # workflow outputs in a flat ``<output_dir>/<workflow>/<file>``
        # layout — matching how _href() builds the <img>/<video> src.
        output_files: list[str] = []
        if r.prompt_result:
            for files in r.prompt_result.outputs.values():
                for of in files:
                    if of.filename:
                        output_files.append(of.filename)
        workflows.append(WorkflowReport(
            name=r.workflow_name,
            passed=r.passed,
            error=r.error,
            execution_time=exec_time,
            output_count=output_count,
            has_baseline=r.has_baseline,
            comparisons=wf_comparisons,
            outputs=output_files,
        ))

    resolved_suite_path = suite_path if suite_path is not None else getattr(
        suite_run, "suite_path", None
    )
    return SuiteReport(
        suite_name=suite_run.suite_name,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        duration=round(suite_run.duration, 2),
        total=suite_run.total,
        passed=suite_run.passed,
        failed=suite_run.failed,
        workflows=workflows,
        target_info=target_info or {},
        timed_out=getattr(suite_run, "timed_out", False),
        aborted_reason=getattr(suite_run, "aborted_reason", None),
        suite_path=str(resolved_suite_path) if resolved_suite_path else None,
    )


# ===================================================================
# Renderers
# ===================================================================

# -------------------------------------------------------------------
# JSON
# -------------------------------------------------------------------

def render_json(report: SuiteReport, indent: int = 2) -> str:
    """Render the report as a JSON string."""
    return json.dumps(report.to_dict(), indent=indent)


# -------------------------------------------------------------------
# Markdown
# -------------------------------------------------------------------

def render_markdown(report: SuiteReport) -> str:
    """Render the report as a GitHub-friendly Markdown string."""
    lines: list[str] = []

    # Header
    status = "✅" if report.failed == 0 else "❌"
    lines.append(f"## {status} Test Report: {report.suite_name}")
    lines.append("")
    lines.append(
        f"**{report.passed}/{report.total} passed** "
        f"| {report.failed} failed "
        f"| {report.duration}s"
    )
    if report.target_info:
        target = report.target_info.get("name", "unknown")
        lines.append(f"| Target: `{target}`")
    lines.append("")

    # Results table
    lines.append("| Workflow | Status | Time | Outputs | Baseline |")
    lines.append("|----------|--------|------|---------|----------|")
    for wf in report.workflows:
        icon = "✅" if wf.passed else "❌"
        time_str = f"{wf.execution_time:.1f}s" if wf.execution_time else "—"
        baseline_str = "✓" if wf.has_baseline else "—"
        error_note = ""
        if wf.error:
            # Truncate long errors for table readability
            short_err = wf.error[:60] + "…" if len(wf.error) > 60 else wf.error
            error_note = f" `{short_err}`"
        lines.append(
            f"| {wf.name} | {icon}{error_note} "
            f"| {time_str} | {wf.output_count} | {baseline_str} |"
        )

    # Comparison details (only if any comparisons were run)
    has_comparisons = any(wf.comparisons for wf in report.workflows)
    if has_comparisons:
        lines.append("")
        lines.append("### Comparison Results")
        lines.append("")
        for wf in report.workflows:
            if not wf.comparisons:
                continue
            lines.append(f"**{wf.name}**")
            lines.append("")
            lines.append("| File | Method | Score | Threshold | Status |")
            lines.append("|------|--------|-------|-----------|--------|")
            for c in wf.comparisons:
                c_icon = "✅" if c.result.passed else "❌"
                score_str = f"{c.result.score:.4f}" if c.result.score is not None else "—"
                thresh_str = str(c.result.threshold) if c.result.threshold is not None else "—"
                lines.append(
                    f"| {c.test_file} | {c.result.method} "
                    f"| {score_str} | {thresh_str} | {c_icon} |"
                )
            lines.append("")

    lines.append(f"*Generated {report.timestamp}*")
    return "\n".join(lines)


# -------------------------------------------------------------------
# Console (Rich)
# -------------------------------------------------------------------

def render_console(report: SuiteReport) -> str:
    """Render the report as a Rich-compatible console string.

    Returns a string with Rich markup.  Callers can print it with
    ``rich.console.Console().print()`` or just ``print()`` (markup
    will show as plain tags).
    """
    lines: list[str] = []

    # Header
    if report.failed == 0:
        lines.append(f"[bold green]✓ {report.suite_name}: "
                      f"all {report.total} passed[/bold green] "
                      f"({report.duration}s)")
    else:
        lines.append(f"[bold red]✗ {report.suite_name}: "
                      f"{report.failed}/{report.total} failed[/bold red] "
                      f"({report.duration}s)")

    if report.target_info:
        lines.append(f"  [dim]Target: {report.target_info.get('name', 'unknown')}[/dim]")

    lines.append("")

    # Per-workflow results
    for wf in report.workflows:
        if wf.passed:
            time_str = f"{wf.execution_time:.1f}s" if wf.execution_time else ""
            lines.append(
                f"  [green]✓[/green] {wf.name}  "
                f"[dim]{wf.output_count} outputs, {time_str}[/dim]"
            )
        else:
            error_msg = wf.error or "unknown error"
            lines.append(f"  [red]✗[/red] {wf.name}  [red]{error_msg}[/red]")

        # Comparison sub-results
        for c in wf.comparisons:
            if c.result.passed:
                score_str = f"{c.result.score:.4f}" if c.result.score is not None else "ok"
                lines.append(
                    f"    [dim]↳ {c.test_file}: "
                    f"{c.result.method} {score_str}[/dim]"
                )
            else:
                score_str = f"{c.result.score:.4f}" if c.result.score is not None else "fail"
                lines.append(
                    f"    [yellow]↳ {c.test_file}: "
                    f"{c.result.method} {score_str} "
                    f"(threshold: {c.result.threshold})[/yellow]"
                )

    return "\n".join(lines)


# -------------------------------------------------------------------
# HTML
# -------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Report: {suite_name}</title>
<style>
  :root {{ --pass: #22c55e; --fail: #ef4444; --warn: #f59e0b; --bg: #0f172a; --fg: #e2e8f0; --card: #1e293b; --border: #334155; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--fg); padding: 2rem; line-height: 1.6; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
  .summary {{ display: flex; gap: 2rem; margin: 1rem 0 2rem; font-size: 1.1rem; }}
  .summary .pass {{ color: var(--pass); }} .summary .fail {{ color: var(--fail); }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1rem; }}
  .card.fail {{ border-left: 4px solid var(--fail); }}
  .card.pass {{ border-left: 4px solid var(--pass); }}
  .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }}
  .card-header h2 {{ font-size: 1.1rem; font-weight: 600; }}
  .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
  .badge.pass {{ background: var(--pass); color: #000; }} .badge.fail {{ background: var(--fail); color: #fff; }}
  .meta {{ color: #94a3b8; font-size: 0.85rem; }}
  .error {{ color: var(--fail); margin-top: 0.5rem; font-family: monospace; font-size: 0.85rem; white-space: pre-wrap; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 0.75rem; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: #94a3b8; font-weight: 500; }}
  .score-pass {{ color: var(--pass); }} .score-fail {{ color: var(--fail); }}
  .img-grid {{ display: flex; flex-direction: column; gap: 16px; margin-top: 12px; }}
  .img-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; align-items: start; }}
  .img-tile {{ background: #0b1220; border: 1px solid var(--border); border-radius: 6px; padding: 8px; }}
  .img-tile a {{ display: block; }}
  .img-tile img,
  .img-tile video {{ width: 100%; height: auto; display: block; border-radius: 4px; background: #000; }}
  .img-tile .label {{ color: #94a3b8; font-size: 0.75rem; margin-top: 6px; word-break: break-all; }}
  .img-tile.baseline {{ border-color: var(--pass); }}
  .img-tile.test {{ border-color: var(--border); }}
  .img-tile.test.fail {{ border-color: var(--fail); }}
  .img-tile.diff {{ border-color: var(--fail); }}
  .timestamp {{ color: #64748b; font-size: 0.8rem; margin-top: 2rem; }}
</style>
</head>
<body>
<h1>{status_icon} Test Report: {suite_name}</h1>
<div class="summary">
  <span class="pass">{passed} passed</span>
  <span class="fail">{failed} failed</span>
  <span>{total} total</span>
  <span>{duration}s</span>
  {target_html}
</div>
{workflow_cards}
<div class="timestamp">Generated {timestamp}</div>
</body>
</html>
"""

_CARD_TEMPLATE = """\
<div class="card {card_class}">
  <div class="card-header">
    <h2>{name}</h2>
    <span class="badge {badge_class}">{badge_text}</span>
  </div>
  <div class="meta">{meta}</div>
  {error_html}
  {comparisons_html}
</div>
"""


def render_html(
    report: SuiteReport,
    artifact_url_prefix: str | None = None,
) -> str:
    """Render the report as an HTML page.

    Image references (workflow outputs and ``*_ssim_diff.png``
    overlays) are emitted as ``<img>`` tags pointing at paths relative
    to the report file (e.g. ``./<workflow>/output_0.png``). When the
    HTML is served over HTTP via the central server's
    ``GET /tests/{id}/artifact/<path>`` route, pass that URL prefix as
    *artifact_url_prefix* to rewrite the ``<img>`` srcs accordingly.
    """
    prefix = (artifact_url_prefix or "").rstrip("/")

    def _href(rel: str) -> str:
        # ``rel`` is the workflow-name + filename relative to the
        # run output dir. When opened as a local file, the report
        # sits at the run-dir root so a relative ``./`` link works.
        # Normalize Windows path separators and URL-encode unsafe
        # characters (spaces, ``#``, ``?``, non-ASCII) so the link
        # works in any browser and over HTTP. The path is then
        # HTML-escaped on top to neutralize ``&``/``<``/``>`` etc.
        rel = rel.replace("\\", "/")
        encoded = urllib.parse.quote(rel, safe="/")
        if not prefix:
            return _html_escape(encoded)
        return _html_escape(f"{prefix}/{encoded}")

    cards: list[str] = []
    for wf in report.workflows:
        card_class = "pass" if wf.passed else "fail"
        badge_class = "pass" if wf.passed else "fail"
        badge_text = "PASS" if wf.passed else "FAIL"

        meta_parts: list[str] = []
        if wf.execution_time is not None:
            meta_parts.append(f"{wf.execution_time:.1f}s")
        meta_parts.append(f"{wf.output_count} outputs")
        if wf.has_baseline:
            meta_parts.append("has baseline")
        meta = " · ".join(meta_parts)

        error_html = ""
        if wf.error:
            error_html = f'<div class="error">{_html_escape(wf.error)}</div>'

        comparisons_html = ""
        if wf.comparisons:
            rows: list[str] = []
            for c in wf.comparisons:
                score_class = "score-pass" if c.result.passed else "score-fail"
                score_str = f"{c.result.score:.4f}" if c.result.score is not None else "—"
                thresh_str = str(c.result.threshold) if c.result.threshold is not None else "—"
                status = "✓" if c.result.passed else "✗"
                rows.append(
                    f"<tr><td>{_html_escape(c.test_file)}</td>"
                    f"<td>{c.result.method}</td>"
                    f'<td class="{score_class}">{score_str}</td>'
                    f"<td>{thresh_str}</td>"
                    f'<td class="{score_class}">{status}</td></tr>'
                )
            comparisons_html = (
                "<table><thead><tr>"
                "<th>File</th><th>Method</th><th>Score</th>"
                "<th>Threshold</th><th>Status</th>"
                "</tr></thead><tbody>"
                + "\n".join(rows)
                + "</tbody></table>"
            )

            # Image grid: per-comparison baseline + test + diff overlay.
            # Render each comparison as its own three-up row so the
            # operator can scan baseline ↔ new ↔ diff at a glance.
            # Video comparisons (e.g. ``video_frame_ssim``) keep the
            # baseline/test tiles as ``<video>`` players so the diff
            # strip alongside is contextualized by the actual clip.
            rows_html: list[str] = []
            for c in wf.comparisons:
                test_is_image = _looks_like_image(c.test_file)
                test_is_video = _looks_like_video(c.test_file)
                diff_artifact = c.result.diff_artifact
                diff_is_image = (
                    bool(diff_artifact)
                    and _looks_like_image(str(diff_artifact))
                )
                if not (test_is_image or test_is_video or diff_is_image):
                    continue

                tiles: list[str] = []
                # Baseline tile — copied into output_dir/_baselines/<wf>/
                # by write_report when suite_path is known.
                baseline_is_image = _looks_like_image(c.baseline_file or "")
                baseline_is_video = _looks_like_video(c.baseline_file or "")
                if c.baseline_file and (baseline_is_image or baseline_is_video):
                    baseline_rel = (
                        f"_baselines/{wf.name}/{c.baseline_file}"
                    )
                    tiles.append(_render_media_tile(
                        css="img-tile baseline",
                        label_prefix="baseline",
                        filename=c.baseline_file,
                        href=_href(baseline_rel),
                        is_video=baseline_is_video,
                    ))
                # Test (new) tile.
                test_tile_class = "img-tile test"
                if not c.result.passed:
                    test_tile_class += " fail"
                if test_is_image or test_is_video:
                    test_rel = f"{wf.name}/{c.test_file}"
                    tiles.append(_render_media_tile(
                        css=test_tile_class,
                        label_prefix="test",
                        filename=c.test_file,
                        href=_href(test_rel),
                        is_video=test_is_video,
                    ))
                # Diff tile (SSIM heatmap, video frame strip, …).
                if diff_is_image:
                    diff_name = Path(str(diff_artifact)).name
                    diff_rel = f"{wf.name}/{diff_name}"
                    tiles.append(
                        f'<div class="img-tile diff">'
                        f'<a href="{_href(diff_rel)}" target="_blank">'
                        f'<img src="{_href(diff_rel)}" alt="diff" loading="lazy"></a>'
                        f'<div class="label">diff: {_html_escape(diff_name)} '
                        f'({c.result.method})</div>'
                        f'</div>'
                    )
                rows_html.append(
                    '<div class="img-row">' + "\n".join(tiles) + "</div>"
                )
            if rows_html:
                comparisons_html += (
                    '<div class="img-grid">' + "\n".join(rows_html) + "</div>"
                )
        elif wf.outputs:
            # No baseline → no comparisons. Still surface every output
            # so the operator can eyeball the run without digging
            # through the file tree. Images render inline, videos get
            # ``<video>`` players, anything else (audio, .safetensors,
            # …) shows up as a download link.
            output_tiles: list[str] = []
            other_links: list[str] = []
            for fname in wf.outputs:
                is_image = _looks_like_image(fname)
                is_video = _looks_like_video(fname)
                rel = f"{wf.name}/{fname}"
                href = _href(rel)
                if is_image or is_video:
                    output_tiles.append(_render_media_tile(
                        css="img-tile test",
                        label_prefix="output",
                        filename=fname,
                        href=href,
                        is_video=is_video,
                    ))
                else:
                    other_links.append(
                        f'<li><a href="{href}" target="_blank">'
                        f'{_html_escape(fname)}</a></li>'
                    )
            if output_tiles:
                comparisons_html += (
                    '<div class="img-grid">'
                    '<div class="img-row">'
                    + "\n".join(output_tiles)
                    + '</div></div>'
                )
            if other_links:
                comparisons_html += (
                    "<ul class=\"outputs\">"
                    + "\n".join(other_links)
                    + "</ul>"
                )

        cards.append(_CARD_TEMPLATE.format(
            card_class=card_class,
            name=_html_escape(wf.name),
            badge_class=badge_class,
            badge_text=badge_text,
            meta=meta,
            error_html=error_html,
            comparisons_html=comparisons_html,
        ))

    status_icon = "✅" if report.failed == 0 else "❌"
    target_html = ""
    if report.target_info:
        target_name = _html_escape(report.target_info.get("name", "unknown"))
        target_html = f"<span>Target: {target_name}</span>"

    return _HTML_TEMPLATE.format(
        suite_name=_html_escape(report.suite_name),
        status_icon=status_icon,
        passed=report.passed,
        failed=report.failed,
        total=report.total,
        duration=report.duration,
        target_html=target_html,
        workflow_cards="\n".join(cards),
        timestamp=report.timestamp,
    )


def _html_escape(s: str) -> str:
    """HTML-escape a string."""
    import html
    return html.escape(s, quote=True)


_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".m4v"})


def _looks_like_image(filename: str) -> bool:
    """Return True if *filename*'s extension is a browser-renderable image."""
    return Path(filename).suffix.lower() in _IMAGE_EXTS


def _looks_like_video(filename: str) -> bool:
    """Return True if *filename*'s extension plays in a ``<video>`` tag."""
    return Path(filename).suffix.lower() in _VIDEO_EXTS


def _render_media_tile(
    *, css: str, label_prefix: str, filename: str, href: str, is_video: bool,
) -> str:
    """Render one tile in the three-up comparison row.

    Images use ``<img loading="lazy">`` so big runs paint progressively;
    videos use ``<video controls preload="metadata">`` so the report
    stays small even when the workflow produced multi-MB clips.
    """
    label = (
        f'<div class="label">{label_prefix}: {_html_escape(filename)}</div>'
    )
    if is_video:
        return (
            f'<div class="{css}">'
            f'<video controls preload="metadata" muted playsinline>'
            f'<source src="{href}">'
            f'</video>'
            f'{label}'
            f'</div>'
        )
    return (
        f'<div class="{css}">'
        f'<a href="{href}" target="_blank">'
        f'<img src="{href}" alt="{label_prefix}" loading="lazy"></a>'
        f'{label}'
        f'</div>'
    )


# ===================================================================
# File writers
# ===================================================================

def write_report(
    report: SuiteReport,
    output_dir: Path,
    formats: list[str] | None = None,
) -> dict[str, Path]:
    """Write the report to files in *output_dir*.

    Args:
        report: The report to write.
        output_dir: Directory to write files into.
        formats: List of formats to write.  Defaults to all.
            Choices: ``json``, ``html``, ``markdown``, ``console``.

    When HTML is requested and ``report.suite_path`` is set, the
    baseline image files referenced by each comparison are copied
    into ``output_dir/_baselines/<workflow>/<file>`` so the HTML
    three-up tile (baseline | test | diff) can render with relative
    paths and remain self-contained when the run directory is moved
    or served over the central server's artifact route.

    Returns a dict mapping format name → written file path.
    """
    if formats is None:
        formats = ["json", "html", "markdown"]

    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    if "json" in formats:
        p = output_dir / "report.json"
        p.write_text(render_json(report), encoding="utf-8")
        written["json"] = p

    if "html" in formats:
        _copy_baselines_for_html(report, output_dir)
        p = output_dir / "report.html"
        p.write_text(render_html(report), encoding="utf-8")
        written["html"] = p

    if "markdown" in formats:
        p = output_dir / "report.md"
        p.write_text(render_markdown(report), encoding="utf-8")
        written["markdown"] = p

    if "console" in formats:
        p = output_dir / "report.txt"
        p.write_text(render_console(report), encoding="utf-8")
        written["console"] = p

    return written


def _copy_baselines_for_html(report: SuiteReport, output_dir: Path) -> None:
    """Copy referenced baseline image files into ``output_dir/_baselines``.

    Baselines normally live in ``{suite_path}/baselines/<workflow>/``.
    For the HTML three-up tile to render them via a relative ``<img>``
    src, they need to be reachable from ``output_dir``.  We copy only
    the baseline files that are actually referenced by a comparison
    entry, and only when the file extension is browser-renderable.
    Existing copies are reused (``Path.exists()`` skip) so repeated
    report writes are cheap.

    Failures are intentionally silent — the table-only fallback in
    the HTML report still works without baselines copied.
    """
    if not report.suite_path:
        return
    suite_dir = Path(report.suite_path)
    baselines_root = suite_dir / "baselines"
    if not baselines_root.is_dir():
        return

    import shutil

    for wf in report.workflows:
        for c in wf.comparisons:
            if not c.baseline_file:
                continue
            if not _looks_like_image(c.baseline_file):
                continue
            src = baselines_root / wf.name / c.baseline_file
            if not src.is_file():
                continue
            dst_dir = output_dir / "_baselines" / wf.name
            dst = dst_dir / c.baseline_file
            if dst.exists():
                continue
            try:
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError:
                # Best-effort: HTML still renders with the test +
                # diff tiles even if the baseline copy fails.
                continue
