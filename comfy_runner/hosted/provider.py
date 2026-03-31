"""Abstract protocol and shared data types for hosted providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PodInfo:
    """Provider-agnostic pod information."""

    id: str
    name: str
    status: str  # "RUNNING", "EXITED", "TERMINATED", etc.
    gpu_type: str = ""
    datacenter: str = ""
    cost_per_hr: float = 0.0
    image: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class VolumeInfo:
    """Provider-agnostic volume information."""

    id: str
    name: str
    size_gb: int
    datacenter: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


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
    ) -> PodInfo: ...

    def start_pod(self, pod_id: str) -> PodInfo: ...

    def stop_pod(self, pod_id: str) -> None: ...

    def terminate_pod(self, pod_id: str) -> None: ...

    def get_pod(self, pod_id: str) -> PodInfo | None: ...

    def list_pods(self) -> list[PodInfo]: ...

    def get_pod_url(self, pod_id: str, port: int) -> str | None: ...

    def create_volume(
        self, name: str, size_gb: int, datacenter: str,
    ) -> VolumeInfo: ...

    def list_volumes(self) -> list[VolumeInfo]: ...

    def get_volume(self, volume_id: str) -> VolumeInfo | None: ...

    def delete_volume(self, volume_id: str) -> None: ...
