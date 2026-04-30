"""Fan-out helpers for fleet-wide operations across discovered comfy-runners.

Currently scoped to a single operation — :func:`fanout_self_update` —
but designed to host any future "do X on every reachable runner"
helper. Targets are dicts produced by
:func:`comfy_runner.hosted.tailnet.discover_comfy_runners` (or
hand-built dicts with at least ``hostname`` and ``host`` keys).

All helpers run requests in parallel via a :class:`ThreadPoolExecutor`
(capped at 8 workers — large enough to overlap network latency, small
enough to avoid hammering pods that share a NAT). Per-target failures
are surfaced in the result list rather than raised, so a single bad
pod doesn't sink the whole sweep.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PORT = 9189
_DEFAULT_TIMEOUT = 60.0
_MAX_WORKERS = 8


def fanout_self_update(
    targets: list[dict[str, Any]],
    *,
    force: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
    port: int = _DEFAULT_PORT,
    scheme: str = "http",
) -> list[dict[str, Any]]:
    """POST ``/self-update`` to every target in parallel.

    Each target dict must have at least:

    * ``hostname`` — short name used for the result row
    * ``host`` — IP or FQDN to address (``server_url`` is also accepted
      and takes precedence when present)

    Returns one result dict per target, in the same order as *targets*::

        {
            "name": "<hostname>",
            "host": "<host or full server_url>",
            "ok": bool,
            "status": int | "EXC",
            "updated": bool,             # only when ok and the pod replied
            "message": str,              # human-readable git output
            "error": str | None,
        }

    Never raises — transport-level failures are recorded with
    ``ok=False`` and ``status="EXC"``.
    """
    if not targets:
        return []

    max_workers = min(_MAX_WORKERS, max(1, len(targets)))

    def _one(target: dict[str, Any]) -> dict[str, Any]:
        return _post_self_update(
            target, force=force, timeout=timeout, port=port, scheme=scheme,
        )

    # `ThreadPoolExecutor.map` runs in parallel but yields results in
    # input order — exactly what callers expect.
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_one, targets))


def _post_self_update(
    target: dict[str, Any],
    *,
    force: bool,
    timeout: float,
    port: int,
    scheme: str,
) -> dict[str, Any]:
    """Single-target POST with structured result dict. Never raises."""
    name = target.get("hostname") or target.get("name") or "?"

    # Prefer an explicit server_url if the target has one (the
    # discovery payload provides it); else build from host+port.
    server_url = target.get("server_url") or ""
    if server_url:
        url = server_url.rstrip("/") + "/self-update"
        host_label = server_url
    else:
        host = target.get("host") or ""
        if not host:
            return {
                "name": name, "host": "", "ok": False, "status": "EXC",
                "updated": False, "message": "",
                "error": "target is missing both 'server_url' and 'host'",
            }
        url = f"{scheme}://{host}:{port}/self-update"
        host_label = host

    try:
        import requests
        resp = requests.post(url, json={"force": bool(force)}, timeout=timeout)
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {"raw": (resp.text or "")[:200]}
    except Exception as e:
        log.warning("self-update fan-out to %s failed: %s", name, e)
        return {
            "name": name, "host": host_label, "ok": False, "status": "EXC",
            "updated": False, "message": "", "error": str(e),
        }

    ok = bool(resp.ok and body.get("ok", False))
    return {
        "name": name,
        "host": host_label,
        "ok": ok,
        "status": resp.status_code,
        "updated": bool(body.get("updated")) if ok else False,
        "message": str(body.get("message") or ""),
        "error": (
            None if ok
            else str(body.get("error") or body.get("raw") or f"HTTP {resp.status_code}")
        ),
    }
