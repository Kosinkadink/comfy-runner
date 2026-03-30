"""RunPod implementation of the HostedProvider protocol."""

from __future__ import annotations

from typing import Any

from .config import get_provider_config, get_runpod_api_key
from .runpod_api import RunPodAPI

DEFAULT_IMAGE = "runpod/ubuntu:24.04"
DEFAULT_PORTS = ["8188/http", "9189/http", "22/tcp"]


class RunPodProvider:
    """High-level RunPod provider used by the CLI."""

    def __init__(self) -> None:
        api_key = get_runpod_api_key()
        if not api_key:
            raise RuntimeError(
                "RunPod API key not set. "
                "Set RUNPOD_API_KEY or configure it via 'comfy-runner hosted config'."
            )
        self.api = RunPodAPI(api_key)
        cfg = get_provider_config("runpod")
        self.default_gpu: str = cfg.get("default_gpu", "NVIDIA L40S")
        self.default_datacenter: str = cfg.get("default_datacenter", "US-KS-2")
        self.default_cloud_type: str = cfg.get("default_cloud_type", "SECURE")

    # ------------------------------------------------------------------
    # Pod methods
    # ------------------------------------------------------------------

    def create_pod(
        self,
        name: str,
        gpu_type: str | None = None,
        image: str | None = None,
        volume_id: str | None = None,
        volume_size_gb: int | None = None,
        ports: list[str] | None = None,
        env: dict[str, str] | None = None,
        datacenter: str | None = None,
        cloud_type: str | None = None,
    ) -> dict[str, Any]:
        """Create a pod with sensible defaults from config."""
        params: dict[str, Any] = {
            "name": name,
            "gpuTypeIds": [gpu_type or self.default_gpu],
            "imageName": image or DEFAULT_IMAGE,
            "ports": ports or DEFAULT_PORTS,
            "containerDiskInGb": 20,
            "volumeMountPath": "/workspace",
            "cloudType": cloud_type or self.default_cloud_type,
        }
        if datacenter or self.default_datacenter:
            params["dataCenterIds"] = [datacenter or self.default_datacenter]
        if volume_id is not None:
            params["networkVolumeId"] = volume_id
        elif volume_size_gb is not None:
            params["volumeInGb"] = volume_size_gb
        if env is not None:
            params["env"] = env
        return self.api.create_pod(**params)

    def start_pod(self, pod_id: str) -> dict[str, Any]:
        """Start a stopped pod."""
        return self.api.start_pod(pod_id)

    def stop_pod(self, pod_id: str) -> None:
        """Stop a running pod."""
        self.api.stop_pod(pod_id)

    def terminate_pod(self, pod_id: str) -> None:
        """Permanently terminate a pod."""
        self.api.terminate_pod(pod_id)

    def get_pod(self, pod_id: str) -> dict[str, Any] | None:
        """Get a single pod by ID."""
        return self.api.get_pod(pod_id)

    def list_pods(self) -> list[dict[str, Any]]:
        """List all pods."""
        return self.api.list_pods()

    def get_pod_url(self, pod_id: str, port: int) -> str | None:
        """Return the proxy URL for a running pod, or ``None``."""
        pod = self.api.get_pod(pod_id)
        if pod is None or pod.get("desiredStatus") != "RUNNING":
            return None
        return f"https://{pod_id}-{port}.proxy.runpod.net"

    # ------------------------------------------------------------------
    # Volume methods
    # ------------------------------------------------------------------

    def create_volume(
        self, name: str, size_gb: int, datacenter: str | None = None,
    ) -> dict[str, Any]:
        """Create a network volume."""
        return self.api.create_volume(
            name, size_gb, datacenter or self.default_datacenter,
        )

    def list_volumes(self) -> list[dict[str, Any]]:
        """List all network volumes."""
        return self.api.list_volumes()

    def get_volume(self, volume_id: str) -> dict[str, Any] | None:
        """Get a single volume by ID."""
        return self.api.get_volume(volume_id)

    def delete_volume(self, volume_id: str) -> None:
        """Delete a network volume."""
        self.api.delete_volume(volume_id)
