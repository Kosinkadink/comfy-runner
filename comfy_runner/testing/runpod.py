"""One-shot deploy-and-test on RunPod ephemeral pods.

Orchestrates: provision → wait → deploy → test → collect → teardown.

The test suite is loaded and executed **locally** — the CLI machine
connects directly to the remote ComfyUI instance (via its proxy or
Tailscale URL) using ``ComfyTestClient``.  This avoids requiring the
suite directory to exist on the pod's filesystem.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from comfy_runner.hosted.config import (
    get_pod_record,
    remove_pod_record,
    set_pod_record,
)
from comfy_runner.hosted.remote import RemoteRunner
from comfy_runner.hosted.runpod_provider import RunPodProvider
from comfy_runner.testing.client import ComfyTestClient
from comfy_runner.testing.report import build_report, write_report
from comfy_runner.testing.runner import run_suite
from comfy_runner.testing.suite import load_suite

_TERMINAL_STATUSES = frozenset({"TERMINATED", "EXITED"})


@dataclass
class RunPodTestConfig:
    """Configuration for a RunPod test run."""

    suite_path: str
    gpu_type: str | None = None
    image: str | None = None
    volume_id: str | None = None
    pr: int | None = None
    branch: str | None = None
    commit: str | None = None
    pod_name: str | None = None
    timeout: int = 600
    http_timeout: int = 30
    formats: str = "json,html,markdown"
    terminate: bool = True
    install_name: str = "main"
    output_dir: str | None = None
    # Suite-level wall-clock budget. Falls back to ``suite.max_runtime_s``
    # loaded from suite.json when None.
    max_runtime_s: int | None = None
    # ``"none"`` | ``"stop"`` | ``"terminate"`` — only ``"terminate"``
    # is meaningful inside ``run_on_runpod`` (it forces teardown of the
    # pod even when ``terminate=False``); other values are honored by
    # the server-side route handler. Defaults to None which behaves as
    # ``"terminate"`` for ephemeral pods.
    on_overrun: str | None = None


@dataclass
class RunPodTestResult:
    """Result of a RunPod test run."""

    pod_id: str
    pod_name: str
    server_url: str
    deploy_result: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    error: str | None = None
    terminated: bool = False
    report: Any = None  # SuiteReport, kept as Any to avoid circular-ish typing
    timed_out: bool = False
    aborted_reason: str | None = None


def _wait_for_server(
    server_url: str,
    timeout: int = 300,
    poll_interval: int = 10,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Poll the server health endpoint until it responds with valid JSON.

    RunPod proxies may return ``200 OK`` HTML pages before the actual
    server is bound.  We require a parseable JSON response from
    ``GET /system-info`` to consider the server truly ready.

    Raises ``RuntimeError`` if the server doesn't respond within *timeout*.
    """
    out = send_output or (lambda _: None)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{server_url}/system-info", timeout=5)
            if resp.ok:
                resp.json()  # raises on non-JSON (proxy HTML pages)
                out("Server is ready.\n")
                return
        except (requests.RequestException, ValueError):
            pass
        remaining = int(deadline - time.monotonic())
        out(f"\rWaiting for server... ({remaining}s remaining)")
        time.sleep(poll_interval)

    raise RuntimeError(f"Server at {server_url} did not become ready within {timeout}s")


def run_on_runpod(
    config: RunPodTestConfig,
    send_output: Callable[[str], None] | None = None,
    cancelled: threading.Event | None = None,
) -> RunPodTestResult:
    """Execute the full provision → deploy → test → teardown lifecycle.

    Tests are executed **locally** — the suite is loaded from the local
    filesystem and workflows are submitted to the remote ComfyUI instance
    via HTTP.  This means the suite directory does NOT need to exist on
    the pod.

    Args:
        config: Test configuration including suite, GPU, deploy target, etc.
        send_output: Optional progress callback for console output.

    Returns a ``RunPodTestResult`` with deployment and test outcomes.
    """
    out = send_output or (lambda _: None)
    provider = RunPodProvider()

    pod_name = config.pod_name or f"test-{int(time.time())}"
    pod_id: str = ""
    server_url: str = ""
    created_pod = False
    result = RunPodTestResult(pod_id="", pod_name=pod_name, server_url="")

    try:
        # ── Step 1: Provision or reuse pod ─────────────────────────
        rec = get_pod_record("runpod", pod_name)
        if rec:
            pod_id = rec["id"]
            out(f"Reusing existing pod '{pod_name}' ({pod_id})\n")
            pod = provider.get_pod(pod_id)
            if not pod or pod.status in _TERMINAL_STATUSES:
                out(f"Pod '{pod_name}' is gone or terminated — creating new pod...\n")
                rec = None  # fall through to creation below

        if not rec:
            out(f"Creating pod '{pod_name}'...\n")
            pod = provider.create_pod(
                name=pod_name,
                gpu_type=config.gpu_type,
                image=config.image,
                volume_id=config.volume_id,
            )
            pod_id = pod.id
            created_pod = True
            set_pod_record("runpod", pod_name, {
                "id": pod_id,
                "gpu_type": pod.gpu_type,
                "datacenter": pod.datacenter,
                "image": pod.image,
                "purpose": "test",
            })
            out(f"Pod created (id: {pod_id}, {pod.gpu_type}, ${pod.cost_per_hr}/hr)\n")
        elif pod and pod.status != "RUNNING":
            out("Starting pod...\n")
            provider.start_pod(pod_id)

        result.pod_id = pod_id

        # Resolve server URL via Tailscale (no public proxy fallback).
        ts_url = provider.get_pod_tailscale_url(pod_name)
        if not ts_url:
            raise RuntimeError(
                f"Cannot resolve server URL for pod '{pod_name}' -- "
                f"Tailscale is not configured. Set tailscale_auth_key "
                f"and tailscale_domain in the runpod provider config."
            )
        server_url = ts_url
        result.server_url = server_url

        # ── Step 2: Wait for server ready ──────────────────────────
        out(f"Waiting for comfy-runner server at {server_url}...\n")
        _wait_for_server(server_url, send_output=send_output)

        runner = RemoteRunner(server_url)

        # ── Step 3: Deploy ─────────────────────────────────────────
        has_deploy = config.pr is not None or config.branch or config.commit

        if has_deploy:
            out(f"Deploying to '{config.install_name}'...\n")
            deploy_data = runner.deploy(
                config.install_name,
                pr=config.pr,
                branch=config.branch,
                commit=config.commit,
                start=True,
            )
            job_id = deploy_data.get("job_id")
            if job_id:
                result.deploy_result = runner.poll_job(
                    job_id,
                    timeout=config.timeout,
                    on_output=send_output,
                )
            else:
                result.deploy_result = deploy_data
            out("Deploy complete.\n")
        else:
            # No deploy requested — just make sure ComfyUI is started
            out("Checking if ComfyUI is running...\n")
            try:
                status = runner.get_status(config.install_name)
                if not status.get("running"):
                    out("Starting ComfyUI...\n")
                    start_data = runner.restart(config.install_name)
                    start_job_id = start_data.get("job_id")
                    if start_job_id:
                        runner.poll_job(start_job_id, timeout=120, on_output=send_output)
            except RuntimeError:
                pass

        # ── Step 4: Run tests locally against remote ComfyUI ───────
        suite = load_suite(config.suite_path)

        # Resolve remote ComfyUI URL (port 8188) via Tailscale.
        comfy_url = ts_url.rsplit(":", 1)[0] + ":8188"
        out(f"Running test suite '{suite.name}' against {comfy_url}\n")

        client = ComfyTestClient(comfy_url, timeout=config.http_timeout)
        run_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        if config.output_dir:
            out_dir = Path(config.output_dir)
        else:
            out_dir = Path(config.suite_path) / "runs" / run_id

        # Suite-level watchdog: body-level override beats suite value.
        # If the caller provided their own ``cancelled`` event we trust
        # them to also manage the Timer (e.g. the server route handler);
        # otherwise we arm our own Timer via the shared context manager.
        if cancelled is not None:
            suite_run = run_suite(
                client, suite, out_dir,
                timeout=config.timeout,
                send_output=send_output,
                cancelled=cancelled,
            )
        else:
            from comfy_runner.testing.client import watchdog as _watchdog
            budget = config.max_runtime_s
            if budget is None:
                suite_budget = getattr(suite, "max_runtime_s", None)
                if isinstance(suite_budget, int):
                    budget = suite_budget

            def _on_abort() -> None:
                out(f"Watchdog: budget {budget}s exceeded — aborting suite\n")
                # Best-effort interrupt of the running ComfyUI prompt.
                try:
                    client.interrupt()
                except Exception:
                    pass

            with _watchdog(budget, on_abort=_on_abort) as local_cancelled:
                suite_run = run_suite(
                    client, suite, out_dir,
                    timeout=config.timeout,
                    send_output=send_output,
                    cancelled=local_cancelled,
                )

        target_info = {"name": pod_name, "url": comfy_url}
        report = build_report(suite_run, target_info=target_info)

        formats_list = [f.strip() for f in config.formats.split(",")]
        write_report(report, out_dir, formats=formats_list)

        result.report = report
        result.timed_out = bool(getattr(suite_run, "timed_out", False))
        result.aborted_reason = getattr(suite_run, "aborted_reason", None)
        result.test_result = {
            "run_id": run_id,
            "suite_name": suite.name,
            "output_dir": str(out_dir),
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "duration": report.duration,
            "timed_out": result.timed_out,
            "aborted_reason": result.aborted_reason,
        }
        if result.timed_out:
            out("Tests aborted by watchdog (overrun).\n")
        else:
            out("Tests complete.\n")

    except KeyboardInterrupt:
        out("\nInterrupted.\n")
        result.error = "Interrupted by user"
    except Exception as e:
        result.error = str(e)
        out(f"Error: {e}\n")
    finally:
        # ── Step 5: Teardown ───────────────────────────────────────
        # Force termination on overrun if the watchdog says so, even
        # when ``terminate=False`` was specified — otherwise a hung
        # ephemeral pod could keep burning GPU after we've given up.
        force_terminate = (
            result.timed_out and config.on_overrun == "terminate"
        )
        if (config.terminate or force_terminate) and pod_id and created_pod:
            try:
                out(f"Terminating pod '{pod_name}'...\n")
                provider.terminate_pod(pod_id)
                result.terminated = True
                out("Pod terminated.\n")
                # Mirror /pods/<name> DELETE: remove the record so the
                # registry doesn't accumulate stale test-pod entries.
                try:
                    remove_pod_record("runpod", pod_name)
                except Exception as re:
                    out(f"Warning: failed to remove pod record: {re}\n")
            except Exception as te:
                out(f"Warning: failed to terminate pod: {te}\n")

    return result
