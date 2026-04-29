"""Tailscale-based discovery of comfy-runner instances.

Wraps the Tailscale REST API (``api.tailscale.com``) so the central
station can enumerate every reachable comfy-runner — RunPod-managed or
not — in one place.

Two layers:

* :func:`list_devices` — a small TTL-cached wrapper over
  ``GET /api/v2/tailnet/{tailnet}/devices``. Returns the raw device
  records or ``[]`` on missing credentials / transport failure.

* :func:`discover_comfy_runners` — probes each online device's
  ``/system-info`` endpoint in parallel, filters to responders, and
  enriches each detected runner with hardware info (from
  ``/system-info``) plus pod metadata (``provider``, ``purpose``, etc.)
  by joining against the configured RunPod pod records.

The hardware extraction is best-effort and known to be light on
non-NVIDIA GPUs (see ``comfy_runner/system_info.py`` ``_get_nvidia_gpus``);
that's tracked as a separate gap.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import (
    get_tailscale_api_key,
    get_tailscale_tailnet,
    list_pod_records,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tailscale device listing — cached
# ---------------------------------------------------------------------------

_DEVICES_CACHE: dict[str, Any] = {
    "at": 0.0, "devices": [], "last_error": None,
}
_DEVICES_TTL = 30.0
_DEVICES_LOCK = threading.Lock()


def list_devices(force: bool = False) -> list[dict[str, Any]]:
    """Return the cached Tailscale device list, refreshing if stale.

    Returns ``[]`` if the API key / tailnet are not configured or the
    HTTP call fails. Cached for ``_DEVICES_TTL`` seconds (30 by default)
    so dashboards / discovery loops don't hammer the API.

    On failure (HTTP non-2xx or transport exception) we **negative-cache**
    for the same TTL — the cache timestamp is updated even though the
    device list isn't, so a Tailscale-API outage doesn't make every
    queued caller pay the full 10 s timeout serially. The error message
    is recorded under ``_DEVICES_CACHE["last_error"]`` and surfaced via
    :func:`get_last_devices_error` so callers (and the dashboard) can
    distinguish a real failure from "0 devices online".
    """
    api_key = get_tailscale_api_key()
    tailnet = get_tailscale_tailnet()
    if not api_key or not tailnet:
        return []

    # Fetch under the lock so concurrent callers don't stampede the
    # Tailscale REST API when the cache goes stale. The fetch is bounded
    # by a 10 s timeout; subsequent queued callers see the just-cached
    # result (success or failure) and return immediately.
    with _DEVICES_LOCK:
        now = time.monotonic()
        if not force and now - _DEVICES_CACHE["at"] < _DEVICES_TTL:
            return list(_DEVICES_CACHE["devices"])

        try:
            import requests
            resp = requests.get(
                f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if not resp.ok:
                msg = f"HTTP {resp.status_code}"
                log.warning("Tailscale device list failed: %s", msg)
                _DEVICES_CACHE["at"] = now
                _DEVICES_CACHE["last_error"] = msg
                # Keep the previously-cached devices (may be []) so the
                # dashboard can still render anything we last knew about.
                return list(_DEVICES_CACHE["devices"])
            devices = resp.json().get("devices", []) or []
        except Exception as e:
            log.warning("Tailscale device list failed: %s", e)
            _DEVICES_CACHE["at"] = now
            _DEVICES_CACHE["last_error"] = str(e)
            return list(_DEVICES_CACHE["devices"])

        _DEVICES_CACHE["at"] = now
        _DEVICES_CACHE["devices"] = devices
        _DEVICES_CACHE["last_error"] = None
        return list(devices)


def get_last_devices_error() -> str | None:
    """Return the error message from the most recent ``list_devices``
    fetch attempt, or ``None`` if the last attempt succeeded (or none
    has happened yet).
    """
    with _DEVICES_LOCK:
        return _DEVICES_CACHE.get("last_error")


def _clear_devices_cache() -> None:
    """Reset the device cache. Test helper."""
    with _DEVICES_LOCK:
        _DEVICES_CACHE["at"] = 0.0
        _DEVICES_CACHE["devices"] = []
        _DEVICES_CACHE["last_error"] = None


# ---------------------------------------------------------------------------
# Hostname helpers — pick the canonical address for probing a device
# ---------------------------------------------------------------------------

def _device_probe_host(device: dict[str, Any]) -> str | None:
    """Pick the most reliable host string to probe for *device*.

    Prefers the first IPv4 (bypasses MagicDNS ambiguity when two devices
    share a hostname), falls back to the device's MagicDNS FQDN.
    Returns ``None`` if neither is available.
    """
    for addr in device.get("addresses", []) or []:
        if addr and "." in addr and ":" not in addr:
            return addr
    fqdn = device.get("name") or ""
    return fqdn or None


def _device_short_hostname(device: dict[str, Any]) -> str:
    """Return the short hostname for *device* (no tailnet suffix)."""
    short = device.get("hostname", "") or ""
    if short:
        return short
    fqdn = device.get("name", "") or ""
    return fqdn.split(".", 1)[0]


def _is_device_online(device: dict[str, Any]) -> bool:
    """Tailscale's online flag — string or bool depending on plan."""
    val = device.get("online")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() == "true"
    return False


# ---------------------------------------------------------------------------
# /system-info probing — confirms comfy-runner AND grabs hardware info
# ---------------------------------------------------------------------------

def probe_system_info(
    host: str,
    port: int = 9189,
    *,
    timeout: float = 2.0,
    scheme: str = "http",
) -> dict[str, Any] | None:
    """Probe a single host's ``/system-info`` endpoint.

    Returns the parsed ``system_info`` payload on success, or ``None``
    on any failure (DNS, connection, non-2xx, malformed JSON, missing
    key). Never raises.
    """
    url = f"{scheme}://{host}:{port}/system-info"
    try:
        import requests
        resp = requests.get(url, timeout=timeout)
        if not resp.ok:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        info = data.get("system_info")
        return info if isinstance(info, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery — enumerate, probe in parallel, enrich
# ---------------------------------------------------------------------------

def _summarise_gpu(system_info: dict[str, Any]) -> str:
    """Best-effort one-line GPU summary from a SystemInfo dict.

    Prefers the first concrete GPU model from ``gpus[]`` (which carries
    vendor/model/vram_mb). Falls back to ``gpu_label`` (vendor only)
    when no per-GPU detail is present (common on non-NVIDIA boxes today
    — see comfy_runner/system_info.py for the gap).
    """
    gpus = system_info.get("gpus") or []
    if isinstance(gpus, list) and gpus:
        first = gpus[0]
        if isinstance(first, dict):
            model = (first.get("model") or "").strip()
            vram = first.get("vram_mb")
            if model and vram:
                return f"{model} ({int(vram)} MB)"
            if model:
                return model
    label = system_info.get("gpu_label") or ""
    return str(label) if label else ""


def _ram_gb(system_info: dict[str, Any]) -> int | None:
    """Pull ``total_memory_gb`` from system info as int, or None."""
    val = system_info.get("total_memory_gb")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _match_pod_record(
    short_hostname: str,
    records: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """Find the runpod pod record (if any) that matches *short_hostname*.

    Pods deployed by ``startup_main.sh`` register on the tailnet as
    ``comfy-{pod_name}`` (with optional ``-1``, ``-2``, ... suffix
    appended by Tailscale on hostname-reclaim drift).

    Match priority:

    1. Exact match against ``base`` — wins when a pod's name itself
       legitimately ends in ``-N`` (e.g. PR pod ``pr-1234``).
    2. Strip a trailing ``-N`` drift suffix and retry — only used when
       the exact match fails.
    """
    if not short_hostname.startswith("comfy-"):
        return None
    base = short_hostname[len("comfy-"):]

    # 1. Exact match wins.
    rec = records.get(base)
    if rec is not None:
        return base, rec

    # 2. Otherwise, try the drift-suffix-stripped form.
    import re
    m = re.match(r"^(.*?)-\d+$", base)
    if m is not None:
        candidate = m.group(1)
        rec = records.get(candidate)
        if rec is not None:
            return candidate, rec
    return None


def discover_comfy_runners(
    *,
    port: int = 9189,
    probe_timeout: float = 2.0,
    scheme: str = "http",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Enumerate every reachable comfy-runner on the tailnet.

    Returns a dict::

        {
            "ok": True,
            "runners": [...],           # one entry per detected runner
            "tailnet_configured": bool, # API key + tailnet both set
            "device_count": int,        # total devices considered
            "online_count": int,        # online devices probed
        }

    Each runner entry::

        {
            "hostname": "comfy-pr-1234",
            "fqdn": "comfy-pr-1234.tailnet.ts.net",
            "host": "100.86.23.124",        # IP used for probing
            "server_url": "http://...:9189",
            "provider": "runpod" | "local",
            "pod_name": "pr-1234" | None,    # only for runpod
            "purpose": "pr" | "persistent" | "test" | None,
            "pr_number": 1234 | None,
            "gpu": "NVIDIA RTX 4090 (24576 MB)",
            "ram_gb": 64,
            "platform": "linux",
            "os": "Ubuntu 22.04",
            "comfy_runner_detected": True,
        }

    Only runners that respond to ``GET /system-info`` are included.
    Devices that fail to probe are silently dropped (the operator's
    cue is the ``device_count`` vs ``len(runners)`` gap).
    """
    devices = list_devices(force=force_refresh)
    error = get_last_devices_error()
    api_key = get_tailscale_api_key()
    tailnet = get_tailscale_tailnet()
    tailnet_configured = bool(api_key and tailnet)

    online = [d for d in devices if _is_device_online(d)]

    pod_records = list_pod_records("runpod")

    runners: list[dict[str, Any]] = []
    if not online:
        return {
            "ok": error is None,
            "runners": runners,
            "tailnet_configured": tailnet_configured,
            "device_count": len(devices),
            "online_count": 0,
            "error": error,
        }

    # Parallel probe — 2s default per device, capped concurrency.
    max_workers = min(16, max(1, len(online)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_device = {}
        for d in online:
            host = _device_probe_host(d)
            if not host:
                continue
            future_to_device[ex.submit(
                probe_system_info, host, port,
                timeout=probe_timeout, scheme=scheme,
            )] = (d, host)

        for fut in as_completed(future_to_device):
            d, host = future_to_device[fut]
            try:
                info = fut.result()
            except Exception:
                info = None
            if info is None:
                continue

            short = _device_short_hostname(d)
            fqdn = d.get("name") or ""
            entry: dict[str, Any] = {
                "hostname": short,
                "fqdn": fqdn,
                "host": host,
                "server_url": f"{scheme}://{host}:{port}",
                "provider": "local",
                "pod_name": None,
                "purpose": None,
                "pr_number": None,
                "gpu": _summarise_gpu(info),
                "ram_gb": _ram_gb(info),
                "platform": info.get("platform") or "",
                "os": info.get("os_release") or info.get("os_distro") or "",
                "comfy_runner_detected": True,
            }

            # ── Join against runpod pod records ────────────────────────
            match = _match_pod_record(short, pod_records)
            if match is not None:
                pod_name, rec = match
                entry["provider"] = "runpod"
                entry["pod_name"] = pod_name
                entry["purpose"] = rec.get("purpose")
                pr = rec.get("pr_number")
                if pr is not None:
                    try:
                        entry["pr_number"] = int(pr)
                    except (TypeError, ValueError):
                        entry["pr_number"] = None
                # Prefer the RunPod-API gpu_type label if recorded
                # (e.g. "RTX 4090") since it's more canonical than what
                # the box self-reports via /system-info.
                if rec.get("gpu_type"):
                    entry["gpu"] = rec["gpu_type"]

            runners.append(entry)

    # Stable order: provider then name.
    runners.sort(key=lambda r: (r["provider"], r["hostname"]))

    return {
        "ok": error is None,
        "runners": runners,
        "tailnet_configured": tailnet_configured,
        "device_count": len(devices),
        "online_count": len(online),
        "error": error,
    }
