"""Abstract protocol for hosted cloud providers."""

from __future__ import annotations

from typing import Any, Protocol


class HostedProvider(Protocol):
    """Protocol that every hosted provider backend must satisfy."""

    def create_pod(
        self,
        name: str,
        gpu_type: str,
        image: str,
        volume_id: str | None = None,
        volume_size_gb: int | None = None,
        ports: list[str] | None = None,
        env: dict[str, str] | None = None,
        datacenter: str | None = None,
        cloud_type: str | None = None,
    ) -> dict[str, Any]: ...

    def start_pod(self, pod_id: str) -> dict[str, Any]: ...

    def stop_pod(self, pod_id: str) -> None: ...

    def terminate_pod(self, pod_id: str) -> None: ...

    def get_pod(self, pod_id: str) -> dict[str, Any] | None: ...

    def list_pods(self) -> list[dict[str, Any]]: ...

    def get_pod_url(self, pod_id: str, port: int) -> str | None: ...

    def create_volume(
        self, name: str, size_gb: int, datacenter: str,
    ) -> dict[str, Any]: ...

    def list_volumes(self) -> list[dict[str, Any]]: ...

    def get_volume(self, volume_id: str) -> dict[str, Any] | None: ...

    def delete_volume(self, volume_id: str) -> None: ...
