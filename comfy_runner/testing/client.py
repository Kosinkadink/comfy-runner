"""ComfyUI test client — queue prompts, poll history, download outputs.

Works with local ComfyUI instances, comfy-runner proxy URLs, or direct
remote endpoints. Uses HTTP polling of ``/history/{id}`` instead of
WebSockets to simplify proxying through the comfy-runner server.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import requests

_TIMEOUT = 30
_POLL_INTERVAL = 2


class WatchdogAborted(RuntimeError):
    """Raised when a watchdog ``cancelled`` Event fires mid-execution."""


@contextmanager
def watchdog(
    budget: int | None,
    on_abort: Callable[[], None] | None = None,
) -> Iterator[threading.Event]:
    """Arm a wall-clock watchdog and yield its ``cancelled`` Event.

    If *budget* is None or non-positive, yields an Event that is never
    set and arms no Timer.

    Otherwise a daemon ``threading.Timer`` is armed for *budget* seconds.
    When it fires, ``cancelled`` is set and *on_abort* (if given) runs
    in the Timer thread; exceptions in *on_abort* are swallowed.

    A ``completed`` flag protected by an internal lock guarantees a
    "first one wins" handoff: when the body of the ``with`` block
    finishes, the flag is set under the lock before the Timer is
    cancelled. If the Timer's callback has not yet entered its critical
    section, it returns without touching ``cancelled``; if it has, the
    main thread will already see ``cancelled.is_set()`` after the
    context exits. This eliminates the race where a normally-completing
    run could be marked aborted because the Timer fired between
    ``timer.cancel()`` being called and the callback actually running.
    """
    cancelled = threading.Event()
    if not isinstance(budget, int) or budget <= 0:
        yield cancelled
        return

    state_lock = threading.Lock()
    completed = False

    def _fire() -> None:
        nonlocal completed
        with state_lock:
            if completed:
                return
            cancelled.set()
        if on_abort is not None:
            try:
                on_abort()
            except Exception:
                pass

    timer = threading.Timer(budget, _fire)
    timer.daemon = True
    timer.start()
    try:
        yield cancelled
    finally:
        with state_lock:
            completed = True
        timer.cancel()


@dataclass
class OutputFile:
    """A single output artifact downloaded from ComfyUI."""

    node_id: str
    filename: str
    subfolder: str
    type: str  # e.g. "output", "temp"
    local_path: Path | None = None


@dataclass
class PromptResult:
    """Result of a completed prompt execution."""

    prompt_id: str
    status: str  # "success" or "error"
    outputs: dict[str, list[OutputFile]] = field(default_factory=dict)
    node_errors: dict[str, Any] = field(default_factory=dict)
    execution_time: float | None = None


class ComfyTestClient:
    """HTTP client for submitting and polling ComfyUI workflows.

    Args:
        base_url: ComfyUI endpoint (e.g. ``http://localhost:8188``).
        timeout: HTTP request timeout in seconds.
        poll_interval: Seconds between history poll attempts.
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = _TIMEOUT,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def queue_prompt(self, workflow: dict[str, Any]) -> str:
        """Submit a workflow to ComfyUI's ``/prompt`` endpoint.

        *workflow* should be an API-format workflow dict (the ``prompt``
        field is added automatically if not already present).

        Returns the ``prompt_id``.
        """
        body: dict[str, Any] = {}
        if "prompt" in workflow:
            body = workflow
        else:
            body["prompt"] = workflow

        try:
            resp = requests.post(
                f"{self.base_url}/prompt",
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to queue prompt ({self.base_url}/prompt): {exc}"
            ) from exc

        data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            error = data.get("error", data.get("node_errors", "unknown"))
            raise RuntimeError(f"ComfyUI rejected prompt: {error}")
        return prompt_id

    # ------------------------------------------------------------------
    # Poll history
    # ------------------------------------------------------------------

    def wait_for_completion(
        self,
        prompt_id: str,
        timeout: int = 600,
        cancelled: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Poll ``/history/{prompt_id}`` until the prompt completes.

        Returns the raw history entry dict.
        Raises ``RuntimeError`` on timeout or execution error.
        Raises ``WatchdogAborted`` if *cancelled* is set during the poll
        loop — used by the suite-level watchdog to abort an in-flight
        workflow.
        """
        deadline = time.monotonic() + timeout

        while True:
            if cancelled is not None and cancelled.is_set():
                # Best-effort cancel of the ComfyUI side as well, so we
                # don't leave the GPU spinning past our budget. The
                # watchdog Timer normally calls this directly via
                # ``client.interrupt()``, but the runner's poll loop
                # also calls it here so callers without direct Timer
                # access (e.g. simple unit tests) still send the
                # interrupt.
                try:
                    self.interrupt()
                except Exception:
                    pass
                raise WatchdogAborted(
                    f"Prompt {prompt_id} aborted by watchdog"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Prompt {prompt_id} timed out after {timeout}s"
                )

            try:
                resp = requests.get(
                    f"{self.base_url}/history/{prompt_id}",
                    timeout=self.timeout,
                )
                resp.raise_for_status()
            except requests.RequestException:
                time.sleep(self.poll_interval)
                continue

            data = resp.json()
            entry = data.get(prompt_id)
            if entry is None:
                # Not in history yet — still executing
                time.sleep(self.poll_interval)
                continue

            status = entry.get("status", {})
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                raise RuntimeError(
                    f"Prompt {prompt_id} execution error: {messages}"
                )

            # Completed (status_str == "success" or outputs are present)
            return entry

    # ------------------------------------------------------------------
    # Interrupt — abort the currently executing prompt
    # ------------------------------------------------------------------

    def interrupt(self) -> bool:
        """Issue ``POST /interrupt`` to ComfyUI to cancel the running prompt.

        Returns ``True`` if the request returned a 2xx response, ``False``
        on any HTTP error (e.g. unreachable server). Never raises — this
        is best-effort signalling from the watchdog.
        """
        try:
            resp = requests.post(
                f"{self.base_url}/interrupt", timeout=self.timeout,
            )
            return resp.ok
        except requests.RequestException:
            return False

    # ------------------------------------------------------------------
    # Output extraction
    # ------------------------------------------------------------------

    def get_outputs(
        self,
        history_entry: dict[str, Any],
    ) -> dict[str, list[OutputFile]]:
        """Extract output file references from a history entry.

        Returns ``{node_id: [OutputFile, ...]}`` for all nodes that
        produced files.
        """
        outputs: dict[str, list[OutputFile]] = {}
        raw_outputs = history_entry.get("outputs", {})

        for node_id, node_out in raw_outputs.items():
            files: list[OutputFile] = []
            # images and gifs come under "images", videos may differ
            for key in ("images", "gifs", "videos", "audio"):
                for item in node_out.get(key, []):
                    files.append(OutputFile(
                        node_id=node_id,
                        filename=item.get("filename", ""),
                        subfolder=item.get("subfolder", ""),
                        type=item.get("type", "output"),
                    ))
            if files:
                outputs[node_id] = files

        return outputs

    def download_output(
        self,
        output_file: OutputFile,
        dest_dir: Path,
    ) -> Path:
        """Download a single output file to *dest_dir*.

        Returns the local path of the downloaded file.
        """
        params: dict[str, str] = {
            "filename": output_file.filename,
            "type": output_file.type,
        }
        if output_file.subfolder:
            params["subfolder"] = output_file.subfolder

        try:
            resp = requests.get(
                f"{self.base_url}/view",
                params=params,
                timeout=self.timeout,
                stream=True,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to download {output_file.filename}: {exc}"
            ) from exc

        dest_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize filename to prevent path traversal from API responses
        safe_name = Path(output_file.filename).name
        dest = dest_dir / safe_name
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        output_file.local_path = dest
        return dest

    def download_all_outputs(
        self,
        history_entry: dict[str, Any],
        dest_dir: Path,
    ) -> dict[str, list[OutputFile]]:
        """Download all output files from a history entry.

        Files are saved to ``dest_dir/{node_id}/``.
        Returns the outputs dict with ``local_path`` populated.
        """
        outputs = self.get_outputs(history_entry)
        for node_id, files in outputs.items():
            node_dir = dest_dir / node_id
            for f in files:
                self.download_output(f, node_dir)
        return outputs

    # ------------------------------------------------------------------
    # Convenience: run a workflow end-to-end
    # ------------------------------------------------------------------

    def run_workflow(
        self,
        workflow: dict[str, Any],
        output_dir: Path,
        timeout: int = 600,
        cancelled: threading.Event | None = None,
    ) -> PromptResult:
        """Queue a workflow, wait for completion, and download outputs.

        Returns a ``PromptResult`` with all outputs downloaded to
        ``output_dir/{node_id}/``.
        """
        prompt_id = self.queue_prompt(workflow)

        t0 = time.monotonic()
        history = self.wait_for_completion(
            prompt_id, timeout=timeout, cancelled=cancelled,
        )
        elapsed = time.monotonic() - t0

        outputs = self.download_all_outputs(history, output_dir)

        status_str = history.get("status", {}).get("status_str", "success")
        node_errors = {}
        for node_id, node_out in history.get("outputs", {}).items():
            if "errors" in node_out:
                node_errors[node_id] = node_out["errors"]

        return PromptResult(
            prompt_id=prompt_id,
            status=status_str,
            outputs=outputs,
            node_errors=node_errors,
            execution_time=elapsed,
        )
