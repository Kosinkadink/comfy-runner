"""RunPod implementation of the HostedProvider protocol."""

from __future__ import annotations

from typing import Any

from comfy_runner.config import get_github_token
from .config import get_provider_config, get_runpod_api_key

import os
from .provider import PodInfo, VolumeInfo
from .runpod_api import RunPodAPI

DEFAULT_IMAGE = "ghcr.io/kosinkadink/comfy-runner:latest"
DEFAULT_PORTS = ["8188/http", "9189/http", "22/tcp"]
DEFAULT_CUDA_VERSIONS = ["12.4", "12.6", "12.8", "13.0"]


def _pod_info(data: dict[str, Any]) -> PodInfo:
    """Map a RunPod pod response to a PodInfo."""
    gpu = data.get("gpu") or {}
    return PodInfo(
        id=data.get("id", ""),
        name=data.get("name", ""),
        status=data.get("desiredStatus", "UNKNOWN"),
        gpu_type=gpu.get("displayName", gpu.get("id", "")),
        datacenter=data.get("machine", {}).get("dataCenterId", ""),
        cost_per_hr=float(data.get("costPerHr") or 0),
        image=data.get("image", ""),
        raw=data,
    )


def _volume_info(data: dict[str, Any]) -> VolumeInfo:
    """Map a RunPod volume response to a VolumeInfo."""
    return VolumeInfo(
        id=data.get("id", ""),
        name=data.get("name", ""),
        size_gb=int(data.get("size") or 0),
        datacenter=data.get("dataCenterId", ""),
        raw=data,
    )


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
        self.default_image: str = cfg.get("default_image", DEFAULT_IMAGE)

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
        allowed_cuda_versions: list[str] | None = None,
        gpu_count: int = 1,
    ) -> PodInfo:
        """Create a pod with sensible defaults from config."""
        params: dict[str, Any] = {
            "name": name,
            "gpuTypeIds": [gpu_type or self.default_gpu],
            "gpuCount": gpu_count,
            "imageName": image or self.default_image,
            "ports": ports or DEFAULT_PORTS,
            "containerDiskInGb": 50,
            "volumeMountPath": "/workspace",
            "cloudType": cloud_type or self.default_cloud_type,
        }
        cuda_vers = allowed_cuda_versions if allowed_cuda_versions is not None else DEFAULT_CUDA_VERSIONS
        if cuda_vers:
            params["allowedCudaVersions"] = cuda_vers
        if datacenter or self.default_datacenter:
            params["dataCenterIds"] = [datacenter or self.default_datacenter]
        if volume_id is not None:
            params["networkVolumeId"] = volume_id
        elif volume_size_gb is not None:
            params["volumeInGb"] = volume_size_gb
        # Build env vars — pass GITHUB_TOKEN for API rate limits
        pod_env: dict[str, str] = {}
        github_token = get_github_token()
        if github_token:
            pod_env["GITHUB_TOKEN"] = github_token
        # Pass Tailscale auth key for automatic tailnet join
        from .config import get_tailscale_auth_key
        ts_auth_key = get_tailscale_auth_key()
        if ts_auth_key:
            pod_env["TAILSCALE_AUTH_KEY"] = ts_auth_key
            # Use pod name as Tailscale hostname for easy identification
            pod_env["TAILSCALE_HOSTNAME"] = f"comfy-{name}"
        if env is not None:
            pod_env.update(env)
        if pod_env:
            params["env"] = pod_env
        return _pod_info(self.api.create_pod(**params))

    def start_pod(self, pod_id: str) -> PodInfo:
        """Start a stopped pod."""
        return _pod_info(self.api.start_pod(pod_id))

    def stop_pod(self, pod_id: str) -> None:
        """Stop a running pod."""
        self.api.stop_pod(pod_id)

    def terminate_pod(self, pod_id: str) -> None:
        """Permanently terminate a pod."""
        self.api.terminate_pod(pod_id)

    def get_pod(self, pod_id: str) -> PodInfo | None:
        """Get a single pod by ID."""
        data = self.api.get_pod(pod_id)
        return _pod_info(data) if data else None

    def list_pods(self) -> list[PodInfo]:
        """List all pods."""
        return [_pod_info(d) for d in self.api.list_pods()]

    def get_pod_url(self, pod_id: str, port: int) -> str | None:
        """Return the proxy URL for a running pod, or ``None``."""
        pod = self.get_pod(pod_id)
        if pod is None or pod.status != "RUNNING":
            return None
        return f"https://{pod_id}-{port}.proxy.runpod.net"

    def get_pod_tailscale_url(self, pod_name: str, port: int = 9189) -> str | None:
        """Return the Tailscale URL for a pod, or None if tailscale is not configured."""
        from .config import get_tailscale_auth_key
        if not get_tailscale_auth_key():
            return None
        ts_domain = get_provider_config("runpod").get("tailscale_domain", "")
        if not ts_domain:
            return None
        # Use HTTP — Tailscale in userspace-networking mode (required on RunPod)
        # doesn't support tailscale serve (HTTPS proxy), but the Tailscale
        # tunnel itself provides encryption.
        return f"http://comfy-{pod_name}.{ts_domain}:{port}"

    # ------------------------------------------------------------------
    # Volume methods
    # ------------------------------------------------------------------

    def create_volume(
        self, name: str, size_gb: int, datacenter: str | None = None,
    ) -> VolumeInfo:
        """Create a network volume."""
        return _volume_info(self.api.create_volume(
            name, size_gb, datacenter or self.default_datacenter,
        ))

    def list_volumes(self) -> list[VolumeInfo]:
        """List all network volumes."""
        return [_volume_info(d) for d in self.api.list_volumes()]

    def get_volume(self, volume_id: str) -> VolumeInfo | None:
        """Get a single volume by ID."""
        data = self.api.get_volume(volume_id)
        return _volume_info(data) if data else None

    def delete_volume(self, volume_id: str) -> None:
        """Delete a network volume."""
        self.api.delete_volume(volume_id)
