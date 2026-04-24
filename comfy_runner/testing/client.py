"""ComfyUI test client — queue prompts, poll history, download outputs.

Works with local ComfyUI instances, comfy-runner proxy URLs, or direct
remote endpoints. Uses HTTP polling of ``/history/{id}`` instead of
WebSockets to simplify proxying through the comfy-runner server.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

_TIMEOUT = 30
_POLL_INTERVAL = 2


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
    ) -> dict[str, Any]:
        """Poll ``/history/{prompt_id}`` until the prompt completes.

        Returns the raw history entry dict.
        Raises ``RuntimeError`` on timeout or execution error.
        """
        deadline = time.monotonic() + timeout

        while True:
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
    ) -> PromptResult:
        """Queue a workflow, wait for completion, and download outputs.

        Returns a ``PromptResult`` with all outputs downloaded to
        ``output_dir/{node_id}/``.
        """
        prompt_id = self.queue_prompt(workflow)

        t0 = time.monotonic()
        history = self.wait_for_completion(prompt_id, timeout=timeout)
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
