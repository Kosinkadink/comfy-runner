"""HTTP client for a remote comfy-runner server.

Wraps the comfy-runner server API so hosted CLI commands can proxy
operations to a pod's server.
"""

from __future__ import annotations

import time
from typing import Any

import requests

_TIMEOUT = 30
_POLL_INTERVAL = 2


class RemoteRunner:
    """Client for a remote comfy-runner server."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _request(
        self, method: str, path: str, **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a request and return the parsed JSON response.

        Raises ``RuntimeError`` on connection errors or non-ok responses.
        """
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", _TIMEOUT)

        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to connect to remote server ({url}): {exc}"
            ) from exc

        try:
            data = resp.json()
        except requests.JSONDecodeError:
            raise RuntimeError(
                f"Remote server returned invalid JSON on {method} {path}: "
                f"{resp.text[:500]}"
            )

        if not resp.ok or not data.get("ok"):
            error = data.get("error", resp.text[:500])
            raise RuntimeError(f"Remote server error: {error}")

        return data

    # ------------------------------------------------------------------
    # Job polling
    # ------------------------------------------------------------------

    def poll_job(
        self,
        job_id: str,
        timeout: int = 600,
        on_output: Any = None,
    ) -> dict[str, Any]:
        """Poll a background job until it completes.

        Args:
            job_id: The job ID to poll.
            timeout: Max seconds to wait (default 600).
            on_output: Optional callback called with new output lines.

        Returns the final job result dict.
        Raises ``RuntimeError`` on job error or timeout.
        """
        seen_lines = 0
        deadline = time.monotonic() + timeout

        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(f"Job {job_id} timed out after {timeout}s")

            data = self._request("GET", f"/job/{job_id}")
            status = data.get("status", "")

            # Emit new output lines
            if on_output:
                output = data.get("output", [])
                for line in output[seen_lines:]:
                    on_output(line)
                seen_lines = len(output)

            if status == "done":
                return data.get("result", {})
            elif status == "error":
                raise RuntimeError(
                    f"Job {job_id} failed: {data.get('error', 'unknown')}"
                )
            elif status == "cancelled":
                raise RuntimeError(f"Job {job_id} was cancelled")

            time.sleep(_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # System info
    # ------------------------------------------------------------------

    def get_system_info(self) -> dict[str, Any]:
        """GET /system-info"""
        data = self._request("GET", "/system-info")
        return data.get("system_info", {})

    # ------------------------------------------------------------------
    # Installations
    # ------------------------------------------------------------------

    def list_installations(self) -> list[dict[str, Any]]:
        """GET /installations"""
        data = self._request("GET", "/installations")
        return data.get("installations", [])

    def get_status(self, name: str) -> dict[str, Any]:
        """GET /{name}/status"""
        return self._request("GET", f"/{name}/status")

    # ------------------------------------------------------------------
    # Deploy
    # ------------------------------------------------------------------

    def deploy(
        self,
        name: str,
        pr: int | None = None,
        branch: str | None = None,
        tag: str | None = None,
        commit: str | None = None,
        reset: bool = False,
        start: bool = False,
        launch_args: str | None = None,
        cuda_compat: bool = False,
    ) -> dict[str, Any]:
        """POST /{name}/deploy — async, returns job data."""
        body: dict[str, Any] = {}
        if pr is not None:
            body["pr"] = pr
        if branch:
            body["branch"] = branch
        if tag:
            body["tag"] = tag
        if commit:
            body["commit"] = commit
        if reset:
            body["reset"] = True
        if start:
            body["start"] = True
        if launch_args is not None:
            body["launch_args"] = launch_args
        if cuda_compat:
            body["cuda_compat"] = True
        return self._request("POST", f"/{name}/deploy", json=body)

    # ------------------------------------------------------------------
    # Process control
    # ------------------------------------------------------------------

    def restart(self, name: str) -> dict[str, Any]:
        """POST /{name}/restart — async."""
        return self._request("POST", f"/{name}/restart")

    def stop(self, name: str) -> dict[str, Any]:
        """POST /{name}/stop — sync."""
        return self._request("POST", f"/{name}/stop")

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def list_nodes(self, name: str) -> list[dict[str, Any]]:
        """GET /{name}/nodes"""
        data = self._request("GET", f"/{name}/nodes")
        return data.get("nodes", [])

    def node_action(self, name: str, action: str, **kwargs: Any) -> dict[str, Any]:
        """POST /{name}/nodes"""
        body = {"action": action, **kwargs}
        return self._request("POST", f"/{name}/nodes", json=body)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def list_snapshots(self, name: str) -> dict[str, Any]:
        """GET /{name}/snapshot"""
        return self._request("GET", f"/{name}/snapshot")

    def save_snapshot(self, name: str, label: str | None = None) -> dict[str, Any]:
        """POST /{name}/snapshot/save"""
        body: dict[str, Any] = {}
        if label:
            body["label"] = label
        return self._request("POST", f"/{name}/snapshot/save", json=body)

    def restore_snapshot(self, name: str, snapshot_id: str) -> dict[str, Any]:
        """POST /{name}/snapshot/restore — async."""
        return self._request(
            "POST", f"/{name}/snapshot/restore", json={"id": snapshot_id},
        )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def list_jobs(self) -> list[dict[str, Any]]:
        """GET /jobs"""
        data = self._request("GET", "/jobs")
        return data.get("jobs", [])

    def get_job(self, job_id: str) -> dict[str, Any]:
        """GET /job/{job_id}"""
        return self._request("GET", f"/job/{job_id}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """POST /job/{job_id}/cancel"""
        return self._request("POST", f"/job/{job_id}/cancel")
