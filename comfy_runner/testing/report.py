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
) -> SuiteReport:
    """Build a ``SuiteReport`` from a ``SuiteRun`` and optional comparisons.

    Args:
        suite_run: Completed test run.
        comparisons: Map of workflow_name → list of ComparisonEntry.
            If None, uses ``suite_run.comparisons`` (populated by
            ``run_suite()`` when baselines are present).
        target_info: Optional metadata about the test target.
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
        workflows.append(WorkflowReport(
            name=r.workflow_name,
            passed=r.passed,
            error=r.error,
            execution_time=exec_time,
            output_count=output_count,
            has_baseline=r.has_baseline,
            comparisons=wf_comparisons,
        ))

    return SuiteReport(
        suite_name=suite_run.suite_name,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        duration=round(suite_run.duration, 2),
        total=suite_run.total,
        passed=suite_run.passed,
        failed=suite_run.failed,
        workflows=workflows,
        target_info=target_info or {},
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


def render_html(report: SuiteReport) -> str:
    """Render the report as a self-contained HTML page."""
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
