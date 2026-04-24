"""Test runner — orchestrates workflow execution across a test suite.

Applies overrides (fixed seeds, resolution changes), manages timeouts,
collects outputs into structured directories, and produces per-run results.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import ComfyTestClient, PromptResult
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
class SuiteRun:
    """Aggregate result of running an entire test suite."""

    suite_name: str
    suite_path: Path
    output_dir: Path
    results: list[WorkflowResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

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
) -> WorkflowResult:
    """Execute a single workflow and download its outputs.

    Args:
        client: ComfyUI test client.
        workflow_path: Path to an API-format workflow JSON file.
        output_dir: Directory to save outputs to.
        overrides: Optional parameter overrides (e.g. ``{"seed": 42}``).
        timeout: Max seconds to wait for completion.
        send_output: Optional progress callback.

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
        result = client.run_workflow(workflow, output_dir, timeout=timeout)
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
) -> SuiteRun:
    """Execute all workflows in a test suite.

    Outputs are saved to ``output_dir/{workflow_stem}/``.

    Args:
        client: ComfyUI test client.
        suite: Loaded test suite.
        output_dir: Root output directory for this run.
        timeout: Per-workflow timeout in seconds.
        send_output: Optional progress callback.

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
        )
        result.has_baseline = suite.has_baseline(wf_name)
        test_run.results.append(result)

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
