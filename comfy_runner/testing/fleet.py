"""Fleet orchestration — parallel test execution across multiple targets.

Runs the same test suite against a heterogeneous fleet of ComfyUI targets
(local URLs, remote pods, ephemeral RunPod instances) in parallel using
``ThreadPoolExecutor``, then aggregates results for cross-target comparison.

Fleet execution is purely client-side — no new server endpoints are needed.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .report import SuiteReport, build_report, render_json, write_report
from .runner import SuiteRun, run_suite
from .suite import Suite, load_suite


# ===================================================================
# Target result
# ===================================================================

@dataclass
class TargetResult:
    """Outcome of running a test suite against one target."""

    target_name: str
    target_kind: str  # "local", "remote", "runpod"
    output_dir: Path | None = None
    duration: float = 0.0
    report: SuiteReport | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        """True if the target ran successfully with no failures."""
        if self.error or self.report is None:
            return False
        return self.report.failed == 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "target_name": self.target_name,
            "target_kind": self.target_kind,
            "duration": round(self.duration, 2),
            "passed": self.passed,
        }
        if self.output_dir is not None:
            d["output_dir"] = str(self.output_dir)
        if self.report is not None:
            d["report"] = self.report.to_dict()
        if self.error is not None:
            d["error"] = self.error
        return d


# ===================================================================
# Fleet result
# ===================================================================

@dataclass
class FleetResult:
    """Aggregate outcome of running a suite across a fleet of targets."""

    suite_name: str
    results: list[TargetResult] = field(default_factory=list)
    total_duration: float = 0.0

    @property
    def total_targets(self) -> int:
        return len(self.results)

    @property
    def targets_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def targets_failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_name": self.suite_name,
            "total_targets": self.total_targets,
            "targets_passed": self.targets_passed,
            "targets_failed": self.targets_failed,
            "total_duration": round(self.total_duration, 2),
            "results": [r.to_dict() for r in self.results],
        }


# ===================================================================
# Target protocol and implementations
# ===================================================================

@runtime_checkable
class TestTarget(Protocol):
    """Protocol for test execution targets."""

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def run(
        self,
        suite: Suite,
        output_dir: Path,
        timeout: int = 600,
        send_output: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> TargetResult: ...


class LocalTarget:
    """Execute tests against a direct ComfyUI URL."""

    def __init__(self, url: str, label: str | None = None) -> None:
        self._url = url
        self._label = label

    @property
    def name(self) -> str:
        return self._label or self._url

    @property
    def kind(self) -> str:
        return "local"

    def run(
        self,
        suite: Suite,
        output_dir: Path,
        timeout: int = 600,
        send_output: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> TargetResult:
        from .client import ComfyTestClient

        t0 = time.monotonic()
        try:
            client = ComfyTestClient(self._url)
            suite_run = run_suite(
                client, suite, output_dir,
                timeout=timeout, send_output=send_output,
                cancelled=cancelled,
            )
            target_info = {"name": self.name, "url": self._url, "kind": self.kind}
            report = build_report(suite_run, target_info=target_info)
            write_report(report, output_dir)
            return TargetResult(
                target_name=self.name,
                target_kind=self.kind,
                output_dir=output_dir,
                duration=time.monotonic() - t0,
                report=report,
            )
        except Exception as e:
            return TargetResult(
                target_name=self.name,
                target_kind=self.kind,
                output_dir=output_dir,
                duration=time.monotonic() - t0,
                error=str(e),
            )


class RemoteTarget:
    """Execute tests against a running pod via its comfy-runner server.

    Uses ``RemoteRunner`` only to check/start ComfyUI.  The actual test
    execution happens locally via ``ComfyTestClient`` pointed at the
    remote ComfyUI URL (port 8188 derived from the server URL).
    """

    def __init__(
        self,
        server_url: str,
        install_name: str = "main",
        label: str | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._install_name = install_name
        self._label = label

    @property
    def name(self) -> str:
        return self._label or self._server_url

    @property
    def kind(self) -> str:
        return "remote"

    def _resolve_comfy_url(self) -> str:
        """Derive the ComfyUI URL (port 8188) from the server URL (port 9189)."""
        url = self._server_url
        # Replace :9189 with :8188 if present
        if ":9189" in url:
            return url.replace(":9189", ":8188")
        # RunPod proxy pattern: pod_id-9189.proxy.runpod.net → pod_id-8188
        if "-9189." in url:
            return url.replace("-9189.", "-8188.")
        # Fallback: append port
        return url.rsplit(":", 1)[0] + ":8188"

    def run(
        self,
        suite: Suite,
        output_dir: Path,
        timeout: int = 600,
        send_output: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> TargetResult:
        from .client import ComfyTestClient
        from comfy_runner.hosted.remote import RemoteRunner

        out = send_output or (lambda _: None)
        t0 = time.monotonic()

        try:
            runner = RemoteRunner(self._server_url)

            # Ensure ComfyUI is running
            try:
                status = runner.get_status(self._install_name)
                if not status.get("running"):
                    out("Starting ComfyUI...\n")
                    start_data = runner.restart(self._install_name)
                    job_id = start_data.get("job_id")
                    if job_id:
                        runner.poll_job(job_id, timeout=120, on_output=send_output)
            except RuntimeError:
                pass

            comfy_url = self._resolve_comfy_url()
            out(f"Running tests against {comfy_url}\n")

            client = ComfyTestClient(comfy_url)
            suite_run = run_suite(
                client, suite, output_dir,
                timeout=timeout, send_output=send_output,
                cancelled=cancelled,
            )
            target_info = {
                "name": self.name,
                "url": comfy_url,
                "server_url": self._server_url,
                "kind": self.kind,
            }
            report = build_report(suite_run, target_info=target_info)
            write_report(report, output_dir)
            return TargetResult(
                target_name=self.name,
                target_kind=self.kind,
                output_dir=output_dir,
                duration=time.monotonic() - t0,
                report=report,
            )
        except Exception as e:
            return TargetResult(
                target_name=self.name,
                target_kind=self.kind,
                output_dir=output_dir,
                duration=time.monotonic() - t0,
                error=str(e),
            )


class EphemeralTarget:
    """Execute tests on an ephemeral RunPod pod.

    Delegates the full provision → deploy → test → teardown lifecycle
    to ``run_on_runpod()``.
    """

    def __init__(
        self,
        gpu_type: str,
        label: str | None = None,
        image: str | None = None,
        volume_id: str | None = None,
        pr: int | None = None,
        branch: str | None = None,
        commit: str | None = None,
        install_name: str = "main",
        terminate: bool = True,
        formats: str = "json,html,markdown",
    ) -> None:
        self._gpu_type = gpu_type
        self._label = label
        self._image = image
        self._volume_id = volume_id
        self._pr = pr
        self._branch = branch
        self._commit = commit
        self._install_name = install_name
        self._terminate = terminate
        self._formats = formats

    @property
    def name(self) -> str:
        return self._label or f"runpod-{self._gpu_type}"

    @property
    def kind(self) -> str:
        return "runpod"

    def run(
        self,
        suite: Suite,
        output_dir: Path,
        timeout: int = 600,
        send_output: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> TargetResult:
        from .runpod import RunPodTestConfig, run_on_runpod

        t0 = time.monotonic()

        config = RunPodTestConfig(
            suite_path=str(suite.path),
            gpu_type=self._gpu_type,
            image=self._image,
            volume_id=self._volume_id,
            pr=self._pr,
            branch=self._branch,
            commit=self._commit,
            timeout=timeout,
            formats=self._formats,
            terminate=self._terminate,
            install_name=self._install_name,
            output_dir=str(output_dir),
        )

        rp_result = run_on_runpod(
            config, send_output=send_output, cancelled=cancelled,
        )
        duration = time.monotonic() - t0

        # Extract report from run_on_runpod result
        report = rp_result.report
        error = rp_result.error

        return TargetResult(
            target_name=self.name,
            target_kind=self.kind,
            output_dir=output_dir,
            duration=duration,
            report=report,
            error=error,
        )


# ===================================================================
# Target spec parsing
# ===================================================================

def parse_target_spec(spec: str) -> TestTarget:
    """Parse a CLI target specification into a ``TestTarget``.

    Supported formats::

        local:<url>                    — direct ComfyUI URL
        remote:<server_url>            — existing pod via comfy-runner server
        runpod:<gpu_type>              — ephemeral RunPod pod

    Each spec may include comma-separated key=value options after the
    primary value::

        remote:https://box.ts.net:9189,install=main,label=office
        runpod:NVIDIA L40S,label=l40s-test

    Raises ``ValueError`` on invalid specs.
    """
    if ":" not in spec:
        raise ValueError(
            f"Invalid target spec: {spec!r}. "
            f"Expected format: local:<url>, remote:<url>, or runpod:<gpu_type>"
        )

    kind, _, rest = spec.partition(":")
    kind = kind.strip().lower()

    if kind not in ("local", "remote", "runpod"):
        raise ValueError(
            f"Unknown target kind: {kind!r}. "
            f"Expected: local, remote, or runpod"
        )

    if not rest.strip():
        raise ValueError(f"Target spec {kind}: requires a value")

    # Parse options from the rest — split on comma, but respect URLs
    # URL part is everything before the first ",key=" pattern
    parts = rest.split(",")
    primary = parts[0].strip()
    options: dict[str, str] = {}

    for part in parts[1:]:
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            options[k.strip()] = v.strip()
        else:
            # If no =, treat as continuation of primary (e.g. GPU names with commas)
            primary += "," + part

    label = options.get("label")

    if kind == "local":
        url = primary
        if "://" not in url:
            url = f"http://{url}"
        return LocalTarget(url=url, label=label)

    elif kind == "remote":
        url = primary
        if "://" not in url:
            url = f"https://{url}"
        install_name = options.get("install", "main")
        return RemoteTarget(
            server_url=url,
            install_name=install_name,
            label=label,
        )

    else:  # runpod
        return EphemeralTarget(
            gpu_type=primary,
            label=label,
            image=options.get("image"),
            volume_id=options.get("volume_id"),
            install_name=options.get("install", "main"),
        )


# ===================================================================
# Fleet runner
# ===================================================================

def _make_safe_dirname(name: str) -> str:
    """Convert a target name into a safe directory component."""
    safe = name.replace("://", "-").replace("/", "-").replace(":", "-")
    safe = safe.replace(" ", "-").replace("\\", "-")
    # Remove any remaining problematic chars
    safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
    return safe.strip("-_.") or "target"


def run_fleet(
    targets: list[TestTarget],
    suite_path: str | Path,
    output_dir: Path,
    timeout: int = 600,
    max_workers: int | None = None,
    send_output: Callable[[str], None] | None = None,
    formats: str = "json,html,markdown",
    cancelled: threading.Event | None = None,
) -> FleetResult:
    """Execute a test suite across multiple targets in parallel.

    Each target gets its own subdirectory under *output_dir*.  Results
    are returned in the same order as *targets* regardless of completion
    order.

    Args:
        targets: List of test targets to run against.
        suite_path: Path to the test suite directory.
        output_dir: Root output directory for this fleet run.
        timeout: Per-workflow timeout in seconds.
        max_workers: ThreadPoolExecutor concurrency (default: number of targets, max 4).
        send_output: Optional progress callback.
        formats: Report formats for per-target reports.
    """
    out = send_output or (lambda _: None)
    lock = threading.Lock()

    suite = load_suite(suite_path)
    fleet_result = FleetResult(suite_name=suite.name)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not targets:
        out("No targets specified.\n")
        return fleet_result

    workers = max_workers or min(len(targets), 4)
    out(f"Fleet run: {len(targets)} target(s), {workers} worker(s), suite '{suite.name}'\n")

    t0 = time.monotonic()

    def _prefixed_output(prefix: str) -> Callable[[str], None]:
        """Create a thread-safe, prefixed output callback."""
        def _out(text: str) -> None:
            with lock:
                out(f"[{prefix}] {text}")
        return _out

    def _run_target(index: int, target: TestTarget) -> tuple[int, TargetResult]:
        dirname = f"{index}-{_make_safe_dirname(target.name)}"
        target_dir = output_dir / dirname
        target_dir.mkdir(parents=True, exist_ok=True)
        target_out = _prefixed_output(target.name)
        target_out(f"Starting ({target.kind})...\n")
        result = target.run(
            suite=suite,
            output_dir=target_dir,
            timeout=timeout,
            send_output=target_out,
            cancelled=cancelled,
        )
        if result.error:
            target_out(f"Error: {result.error}\n")
        else:
            target_out(f"Done ({result.duration:.1f}s)\n")
        return index, result

    # Run targets in parallel, preserving input order
    indexed_results: dict[int, TargetResult] = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_target, i, t): i
            for i, t in enumerate(targets)
        }

        try:
            for future in as_completed(futures):
                try:
                    index, result = future.result()
                    indexed_results[index] = result
                except Exception as e:
                    idx = futures[future]
                    target = targets[idx]
                    indexed_results[idx] = TargetResult(
                        target_name=target.name,
                        target_kind=target.kind,
                        error=str(e),
                    )
        except KeyboardInterrupt:
            out("\nInterrupted — cancelling pending targets...\n")
            for f in futures:
                f.cancel()
            # Collect any results that completed before interrupt
            for f in futures:
                if f.done() and not f.cancelled():
                    try:
                        index, result = f.result()
                        indexed_results[index] = result
                    except Exception:
                        pass
            # Fill in missing targets as interrupted
            for i, t in enumerate(targets):
                if i not in indexed_results:
                    indexed_results[i] = TargetResult(
                        target_name=t.name,
                        target_kind=t.kind,
                        error="Interrupted by user",
                    )

    fleet_result.results = [indexed_results[i] for i in range(len(targets))]
    fleet_result.total_duration = time.monotonic() - t0

    # Write fleet summary
    _write_fleet_summary(fleet_result, output_dir, formats)

    return fleet_result


def _write_fleet_summary(
    fleet_result: FleetResult,
    output_dir: Path,
    formats: str = "json",
) -> None:
    """Write the fleet-level summary files."""
    import json

    output_dir.mkdir(parents=True, exist_ok=True)

    fmt_list = [f.strip() for f in formats.split(",")]

    if "json" in fmt_list:
        path = output_dir / "fleet-report.json"
        path.write_text(
            json.dumps(fleet_result.to_dict(), indent=2),
            encoding="utf-8",
        )


# ===================================================================
# Fleet renderers
# ===================================================================

def render_fleet_console(fleet_result: FleetResult) -> str:
    """Render a fleet result as a Rich-compatible console string."""
    lines: list[str] = []

    # Header
    passed = fleet_result.targets_passed
    total = fleet_result.total_targets
    failed = fleet_result.targets_failed

    if failed == 0:
        lines.append(
            f"[bold green]✓ Fleet: all {total} target(s) passed[/bold green] "
            f"({fleet_result.total_duration:.1f}s)"
        )
    else:
        lines.append(
            f"[bold red]✗ Fleet: {failed}/{total} target(s) failed[/bold red] "
            f"({fleet_result.total_duration:.1f}s)"
        )

    lines.append(f"  Suite: {fleet_result.suite_name}")
    lines.append("")

    # Per-target summary
    for r in fleet_result.results:
        if r.error:
            lines.append(
                f"  [red]✗[/red] {r.target_name} ({r.target_kind})  "
                f"[red]{r.error}[/red]"
            )
        elif r.report:
            rp = r.report
            status_icon = "[green]✓[/green]" if rp.failed == 0 else "[red]✗[/red]"
            lines.append(
                f"  {status_icon} {r.target_name} ({r.target_kind})  "
                f"[dim]{rp.passed}/{rp.total} passed, {r.duration:.1f}s[/dim]"
            )
        else:
            lines.append(
                f"  [yellow]?[/yellow] {r.target_name} ({r.target_kind})  "
                f"[dim]no result[/dim]"
            )

    return "\n".join(lines)


def render_fleet_json(fleet_result: FleetResult, indent: int = 2) -> str:
    """Render the fleet result as a JSON string."""
    import json
    return json.dumps(fleet_result.to_dict(), indent=indent)
