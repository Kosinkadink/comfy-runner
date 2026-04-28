"""Test runner — orchestrates workflow execution across a test suite.

Applies overrides (fixed seeds, resolution changes), manages timeouts,
collects outputs into structured directories, and produces per-run results.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import (
    ComfyTestClient,
    OutputFile,
    PromptResult,
    WatchdogAborted,
)
from .compare.registry import CompareResult, _guess_mimetype, compare_outputs
from .suite import Suite


@dataclass
class WorkflowResult:
    """Result of executing a single workflow."""

    workflow_name: str
    workflow_path: Path
    prompt_result: PromptResult | None = None
    output_dir: Path | None = None
    error: str | None = None
    has_baseline: bool = False

    @property
    def passed(self) -> bool:
        return self.prompt_result is not None and self.error is None


@dataclass
class ComparisonEntry:
    """A single baseline-vs-test file comparison result."""

    baseline_file: str
    test_file: str
    result: CompareResult


@dataclass
class SuiteRun:
    """Aggregate result of running an entire test suite."""

    suite_name: str
    suite_path: Path
    output_dir: Path
    results: list[WorkflowResult] = field(default_factory=list)
    comparisons: dict[str, list[ComparisonEntry]] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0
    # Set to True if the suite-level watchdog aborted the run because
    # ``max_runtime_s`` was exceeded. ``aborted_reason`` carries the
    # human-readable message (e.g. "overrun: budget 60s").
    timed_out: bool = False
    aborted_reason: str | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    @property
    def duration(self) -> float:
        return self.finished_at - self.started_at

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of the run."""
        return {
            "suite": self.suite_name,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "duration": round(self.duration, 2),
            "timed_out": self.timed_out,
            "aborted_reason": self.aborted_reason,
            "results": [
                {
                    "workflow": r.workflow_name,
                    "passed": r.passed,
                    "error": r.error,
                    "execution_time": (
                        round(r.prompt_result.execution_time, 2)
                        if r.prompt_result and r.prompt_result.execution_time
                        else None
                    ),
                    "has_baseline": r.has_baseline,
                    "output_count": (
                        sum(len(files) for files in r.prompt_result.outputs.values())
                        if r.prompt_result
                        else 0
                    ),
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Workflow override helpers
# ---------------------------------------------------------------------------

def _apply_overrides(workflow: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply parameter overrides to a workflow.

    Supports overriding ``seed`` and ``noise_seed`` across all nodes that
    have them.  Other overrides are ignored for now (extensible later).
    """
    seed = overrides.get("seed")
    if seed is not None:
        for node in workflow.values():
            inputs = node.get("inputs", {})
            if "seed" in inputs:
                inputs["seed"] = seed
            if "noise_seed" in inputs:
                inputs["noise_seed"] = seed
    return workflow


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_workflow(
    client: ComfyTestClient,
    workflow_path: Path,
    output_dir: Path,
    overrides: dict[str, Any] | None = None,
    timeout: int = 600,
    send_output: Callable[[str], None] | None = None,
    cancelled: threading.Event | None = None,
) -> WorkflowResult:
    """Execute a single workflow and download its outputs.

    Args:
        client: ComfyUI test client.
        workflow_path: Path to an API-format workflow JSON file.
        output_dir: Directory to save outputs to.
        overrides: Optional parameter overrides (e.g. ``{"seed": 42}``).
        timeout: Max seconds to wait for completion.
        send_output: Optional progress callback.
        cancelled: Optional watchdog cancellation event. When set, the
            in-flight workflow's poll loop raises ``WatchdogAborted``;
            this function catches it and returns a result with
            ``error="overrun"``.

    Returns a ``WorkflowResult``.
    """
    out = send_output or (lambda _: None)
    name = workflow_path.stem

    try:
        with open(workflow_path) as f:
            workflow = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return WorkflowResult(
            workflow_name=name,
            workflow_path=workflow_path,
            error=f"Failed to load workflow: {exc}",
        )

    if overrides:
        workflow = _apply_overrides(workflow, overrides)

    out(f"  Running {name}...")

    try:
        result = client.run_workflow(
            workflow, output_dir, timeout=timeout, cancelled=cancelled,
        )
    except WatchdogAborted as exc:
        out(f" ABORTED ({exc})\n")
        return WorkflowResult(
            workflow_name=name,
            workflow_path=workflow_path,
            error="overrun",
        )
    except RuntimeError as exc:
        out(f" FAILED ({exc})\n")
        return WorkflowResult(
            workflow_name=name,
            workflow_path=workflow_path,
            error=str(exc),
        )

    output_count = sum(len(files) for files in result.outputs.values())
    elapsed = f"{result.execution_time:.1f}s" if result.execution_time else "?"
    out(f" done ({output_count} outputs, {elapsed})\n")

    return WorkflowResult(
        workflow_name=name,
        workflow_path=workflow_path,
        prompt_result=result,
        output_dir=output_dir,
    )


def run_suite(
    client: ComfyTestClient,
    suite: Suite,
    output_dir: Path,
    timeout: int = 600,
    send_output: Callable[[str], None] | None = None,
    cancelled: threading.Event | None = None,
) -> SuiteRun:
    """Execute all workflows in a test suite.

    Outputs are saved to ``output_dir/{workflow_stem}/``.

    Args:
        client: ComfyUI test client.
        suite: Loaded test suite.
        output_dir: Root output directory for this run.
        timeout: Per-workflow timeout in seconds.
        send_output: Optional progress callback.
        cancelled: Optional watchdog cancellation event. Checked between
            workflows and threaded into ``run_workflow`` so the in-flight
            workflow's poll loop can abort. When set, the remaining
            workflows are skipped, ``timed_out=True`` is recorded, and a
            synthetic ``error="overrun"`` row is appended so CI sees a
            non-zero outcome even if every started workflow happened to
            pass.

    Returns a ``SuiteRun`` with all results.
    """
    out = send_output or (lambda _: None)
    overrides = suite.get_overrides()
    test_run = SuiteRun(
        suite_name=suite.name,
        suite_path=suite.path,
        output_dir=output_dir,
        started_at=time.monotonic(),
    )

    out(f"Running suite: {suite.name} ({len(suite.workflows)} workflows)\n")

    for i, wf_path in enumerate(suite.workflows, 1):
        if cancelled is not None and cancelled.is_set():
            out(f"[{i}/{len(suite.workflows)}] aborted before start\n")
            break
        wf_name = wf_path.stem
        out(f"[{i}/{len(suite.workflows)}]")

        wf_output_dir = output_dir / wf_name
        result = run_workflow(
            client,
            wf_path,
            wf_output_dir,
            overrides=overrides,
            timeout=timeout,
            send_output=send_output,
            cancelled=cancelled,
        )
        result.has_baseline = suite.has_baseline(wf_name)
        test_run.results.append(result)
        if cancelled is not None and cancelled.is_set():
            # The current workflow was either aborted by the watchdog
            # (already recorded as error="overrun") or finished just
            # before the event fired. Either way, stop here.
            break

    # If the watchdog fired, record overrun status and append a synthetic
    # failure row when none of the existing rows already capture it (so
    # the report's failed count is always > 0).
    if cancelled is not None and cancelled.is_set():
        test_run.timed_out = True
        test_run.aborted_reason = "overrun"
        if not any(r.error == "overrun" for r in test_run.results):
            test_run.results.append(WorkflowResult(
                workflow_name="__watchdog__",
                workflow_path=Path("__watchdog__"),
                error="overrun",
            ))

    # ── Compare outputs against baselines ──────────────────────────
    for result in test_run.results:
        wf_name = result.workflow_name
        if not result.passed or not result.has_baseline or not result.output_dir:
            continue
        baseline_files = suite.get_baseline_files(wf_name)
        if not baseline_files:
            continue

        # Build map of filename → test output path across all nodes
        test_files: dict[str, Path] = {}
        if result.prompt_result:
            for files in result.prompt_result.outputs.values():
                for f in files:
                    if f.local_path and f.local_path.is_file():
                        test_files[f.local_path.name] = f.local_path

        entries: list[ComparisonEntry] = []
        for bl_path in baseline_files:
            test_path = test_files.get(bl_path.name)
            if test_path is None:
                # Try matching by position if names don't align
                continue
            mimetype = _guess_mimetype(bl_path)
            cfg = suite.get_compare_config(mimetype)
            cmp_result = compare_outputs(bl_path, test_path, cfg)
            entries.append(ComparisonEntry(
                baseline_file=bl_path.name,
                test_file=test_path.name,
                result=cmp_result,
            ))

        if entries:
            test_run.comparisons[wf_name] = entries

    test_run.finished_at = time.monotonic()

    passed = test_run.passed
    failed = test_run.failed
    out(f"\nResults: {passed} passed, {failed} failed ({test_run.duration:.1f}s)\n")

    # Write summary JSON
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(test_run.summary(), f, indent=2)

    return test_run
