"""Hosted provider credentials and volume registry.

Lives under the ``"hosted"`` key in the main comfy-runner config JSON.
"""

from __future__ import annotations

import os
from typing import Any

from comfy_runner.config import load_config, save_config

DEFAULT_RUNPOD_CONFIG: dict[str, Any] = {
    "api_key": "",
    "s3_access_key": "",
    "s3_secret_key": "",
    "default_gpu": "NVIDIA L40S",
    "default_datacenter": "US-KS-2",
    "default_cloud_type": "SECURE",
    "cache_releases": 3,
    "volumes": {},
}


def get_hosted_config() -> dict[str, Any]:
    """Return the full hosted config dict, defaulting to ``{}``."""
    config = load_config()
    return config.get("hosted", {})


def get_provider_config(provider: str) -> dict[str, Any]:
    """Return config for a single hosted provider (e.g. ``"runpod"``)."""
    return get_hosted_config().get(provider, {})


def set_provider_config(provider: str, data: dict[str, Any]) -> None:
    """Set the entire config dict for a hosted provider."""
    config = load_config()
    hosted = config.setdefault("hosted", {})
    hosted[provider] = data
    save_config(config)


_INT_KEYS = frozenset({"cache_releases"})
_RESERVED_KEYS = frozenset({"volumes", "pods"})


def set_provider_value(provider: str, key: str, value: str) -> None:
    """Set a single key within a provider's config.

    Supports dotted keys like ``"default_gpu"``—each dot-separated
    segment navigates one level deeper into the nested dict.

    Raises ``ValueError`` if the target key is a reserved namespace
    (e.g. ``volumes``) that cannot be overwritten with a scalar.
    """
    config = load_config()
    hosted = config.setdefault("hosted", {})
    prov = hosted.setdefault(provider, {})
    parts = key.split(".")
    target = prov
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    final_key = parts[-1]
    if final_key in _RESERVED_KEYS:
        raise ValueError(
            f"Cannot overwrite '{final_key}' — use the dedicated "
            f"volume commands instead."
        )
    # Cast known int keys
    coerced: str | int = value
    if final_key in _INT_KEYS:
        try:
            coerced = int(value)
        except ValueError:
            pass
    target[final_key] = coerced
    save_config(config)


def get_volume_config(provider: str, volume_name: str) -> dict[str, Any] | None:
    """Return a named volume's config, or ``None`` if it doesn't exist."""
    volumes = get_provider_config(provider).get("volumes", {})
    return volumes.get(volume_name)


def set_volume_config(provider: str, volume_name: str, data: dict[str, Any]) -> None:
    """Create or update a named volume's config."""
    config = load_config()
    hosted = config.setdefault("hosted", {})
    prov = hosted.setdefault(provider, {})
    volumes = prov.setdefault("volumes", {})
    volumes[volume_name] = data
    save_config(config)


def remove_volume_config(provider: str, volume_name: str) -> bool:
    """Remove a volume entry. Returns ``True`` if it existed."""
    config = load_config()
    volumes = config.get("hosted", {}).get(provider, {}).get("volumes", {})
    if volume_name in volumes:
        del volumes[volume_name]
        save_config(config)
        return True
    return False


def list_volume_configs(provider: str) -> dict[str, dict[str, Any]]:
    """Return all volumes for a provider."""
    return get_provider_config(provider).get("volumes", {})


def get_runpod_api_key() -> str:
    """Get RunPod API key from env var ``RUNPOD_API_KEY``, then config."""
    token = os.environ.get("RUNPOD_API_KEY", "")
    if token:
        return token
    return get_provider_config("runpod").get("api_key", "")


# ---------------------------------------------------------------------------
# Pod registry — track created pods by name
# ---------------------------------------------------------------------------

def get_pod_record(provider: str, pod_name: str) -> dict[str, Any] | None:
    """Return a named pod's record, or ``None`` if it doesn't exist."""
    pods = get_provider_config(provider).get("pods", {})
    return pods.get(pod_name)


def set_pod_record(provider: str, pod_name: str, data: dict[str, Any]) -> None:
    """Create or update a named pod's record."""
    config = load_config()
    hosted = config.setdefault("hosted", {})
    prov = hosted.setdefault(provider, {})
    pods = prov.setdefault("pods", {})
    pods[pod_name] = data
    save_config(config)


def remove_pod_record(provider: str, pod_name: str) -> bool:
    """Remove a pod record. Returns ``True`` if it existed."""
    config = load_config()
    pods = config.get("hosted", {}).get(provider, {}).get("pods", {})
    if pod_name in pods:
        del pods[pod_name]
        save_config(config)
        return True
    return False


def list_pod_records(provider: str) -> dict[str, dict[str, Any]]:
    """Return all pod records for a provider."""
    return get_provider_config(provider).get("pods", {})
