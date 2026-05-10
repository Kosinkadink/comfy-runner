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

    import requests

    # The HTTPS-required signature is shared with discovery; reuse the
    # detector so both code paths upgrade on the same condition.
    from .tailnet import looks_like_https_required

    payload = {"force": bool(force)}

    def _do_post(post_url: str) -> tuple[Any, dict[str, Any] | None, str | None]:
        """Returns (response, parsed_body_or_None, transport_error_or_None)."""
        try:
            r = requests.post(post_url, json=payload, timeout=timeout)
        except Exception as e:
            return None, None, str(e)
        try:
            b = r.json() if r.content else {}
        except Exception:
            b = {"raw": (r.text or "")[:200]}
        return r, b, None

    resp, body, transport_err = _do_post(url)
    if transport_err is not None:
        log.warning("self-update fan-out to %s failed: %s", name, transport_err)
        return {
            "name": name, "host": host_label, "ok": False, "status": "EXC",
            "updated": False, "message": "", "error": transport_err,
        }

    # HTTPS-fronted runner served us the Go "client sent an HTTP request
    # to an HTTPS server" 400. Retry over HTTPS+FQDN if we know one — the
    # cert SAN list contains the MagicDNS name, not the IP.
    if (
        url.startswith("http://")
        and looks_like_https_required(resp)
        and (target.get("fqdn") or "")
    ):
        fqdn = target["fqdn"]
        retry_url = f"https://{fqdn}:{port}/self-update"
        log.info(
            "self-update %s: HTTP listener requires HTTPS; retrying %s",
            name, retry_url,
        )
        retry_resp, retry_body, retry_err = _do_post(retry_url)
        if retry_err is not None:
            log.warning(
                "self-update fan-out HTTPS retry to %s failed: %s",
                name, retry_err,
            )
            return {
                "name": name, "host": retry_url, "ok": False, "status": "EXC",
                "updated": False, "message": "", "error": retry_err,
            }
        resp, body, host_label = retry_resp, retry_body, retry_url

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
