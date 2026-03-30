"""RunPod REST API client.

Thin wrapper around ``https://rest.runpod.io/v1/``.
"""

from __future__ import annotations

from typing import Any

import requests

BASE_URL = "https://rest.runpod.io/v1"
_TIMEOUT = 30


class RunPodAPI:
    """Low-level HTTP client for the RunPod REST API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        """Return auth headers for every request."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, path: str, **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Make a request to ``BASE_URL + path``.

        Returns parsed JSON on 2xx (``None`` for 204).
        Raises ``RuntimeError`` on 4xx/5xx or connection errors.
        """
        url = f"{BASE_URL}{path}"
        kwargs.setdefault("headers", self._headers())
        kwargs.setdefault("timeout", _TIMEOUT)

        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to connect to RunPod API ({url}): {exc}"
            ) from exc

        if resp.status_code == 204:
            return None

        if resp.ok:
            try:
                return resp.json()
            except requests.JSONDecodeError as exc:
                raise RuntimeError(
                    f"RunPod API returned invalid JSON on {method} {path}: "
                    f"{resp.text[:200]}"
                ) from exc

        raise RuntimeError(
            f"RunPod API error {resp.status_code} on {method} {path}: "
            f"{resp.text}"
        )

    # ------------------------------------------------------------------
    # Pod methods
    # ------------------------------------------------------------------

    def create_pod(self, **kwargs: Any) -> dict[str, Any]:
        """``POST /pods`` — create a new pod."""
        return self._request("POST", "/pods", json=kwargs)  # type: ignore[return-value]

    def list_pods(self) -> list[dict[str, Any]]:
        """``GET /pods`` — list all pods."""
        return self._request("GET", "/pods") or []  # type: ignore[return-value]

    def get_pod(self, pod_id: str) -> dict[str, Any] | None:
        """``GET /pods/{pod_id}`` — get a single pod."""
        return self._request("GET", f"/pods/{pod_id}")

    def start_pod(self, pod_id: str) -> dict[str, Any]:
        """``POST /pods/{pod_id}/start`` — start a stopped pod."""
        return self._request("POST", f"/pods/{pod_id}/start")  # type: ignore[return-value]

    def stop_pod(self, pod_id: str) -> None:
        """``POST /pods/{pod_id}/stop`` — stop a running pod."""
        self._request("POST", f"/pods/{pod_id}/stop")

    def terminate_pod(self, pod_id: str) -> None:
        """``DELETE /pods/{pod_id}`` — permanently terminate a pod."""
        self._request("DELETE", f"/pods/{pod_id}")

    # ------------------------------------------------------------------
    # Volume methods
    # ------------------------------------------------------------------

    def create_volume(
        self, name: str, size: int, datacenter_id: str,
    ) -> dict[str, Any]:
        """``POST /networkvolumes`` — create a network volume."""
        return self._request(  # type: ignore[return-value]
            "POST",
            "/networkvolumes",
            json={"name": name, "size": size, "dataCenterId": datacenter_id},
        )

    def list_volumes(self) -> list[dict[str, Any]]:
        """``GET /networkvolumes`` — list all network volumes."""
        return self._request("GET", "/networkvolumes") or []  # type: ignore[return-value]

    def get_volume(self, volume_id: str) -> dict[str, Any] | None:
        """``GET /networkvolumes/{volume_id}`` — get a single volume."""
        return self._request("GET", f"/networkvolumes/{volume_id}")

    def delete_volume(self, volume_id: str) -> None:
        """``DELETE /networkvolumes/{volume_id}`` — delete a network volume."""
        self._request("DELETE", f"/networkvolumes/{volume_id}")

    # ------------------------------------------------------------------
    # Template methods
    # ------------------------------------------------------------------

    def create_template(self, **kwargs: Any) -> dict[str, Any]:
        """``POST /templates`` — create a pod template."""
        return self._request("POST", "/templates", json=kwargs)  # type: ignore[return-value]

    def list_templates(self) -> list[dict[str, Any]]:
        """``GET /templates`` — list all templates."""
        return self._request("GET", "/templates") or []  # type: ignore[return-value]
