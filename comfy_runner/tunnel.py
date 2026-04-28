"""Tunnel exposure: ngrok and tailscale providers.

Provides a Protocol-based provider abstraction with two concrete
implementations (NgrokTunnel, TailscaleTunnel) plus high-level helpers
that integrate with the installation registry and process state.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from .config import CONFIG_DIR
from .process import get_status, is_process_alive, kill_process_tree

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


# ---------------------------------------------------------------------------
# Tunnel state files — ~/.comfy-runner/tunnel-{port}.json
# ---------------------------------------------------------------------------

def _tunnel_state_path(port: int) -> Path:
    return CONFIG_DIR / f"tunnel-{port}.json"


def _write_tunnel_state(
    port: int, pid: int, url: str, provider: str, **extra: Any,
) -> None:
    from safe_file import atomic_write
    data = {"pid": pid, "url": url, "provider": provider, "port": port, **extra}
    atomic_write(_tunnel_state_path(port), json.dumps(data) + "\n")


def _read_tunnel_state(port: int) -> dict[str, Any] | None:
    path = _tunnel_state_path(port)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _remove_tunnel_state(port: int) -> None:
    _tunnel_state_path(port).unlink(missing_ok=True)


def _all_tunnel_states() -> list[dict[str, Any]]:
    """Read all active tunnel state files."""
    states = []
    for p in CONFIG_DIR.glob("tunnel-*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                states.append(data)
        except (json.JSONDecodeError, OSError):
            pass
    return states


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

class TunnelProvider(Protocol):
    def start(self, port: int) -> str: ...      # Returns public URL
    def stop(self) -> None: ...
    def get_url(self) -> str | None: ...


# ---------------------------------------------------------------------------
# NgrokTunnel
# ---------------------------------------------------------------------------

class NgrokTunnel:
    """Tunnel provider using the ngrok binary.

    Reads ``tunnel.ngrok`` from ``~/.comfy-runner/config.json``::

        {
          "authtoken": "2abc...",
          "domains": ["comfy-1.ngrok-free.app", "comfy-2.ngrok-free.app"],
          "region": "us"
        }

    When *domains* are configured, the first unused domain is allocated for
    each tunnel.  Each ngrok process gets its own ``--api-addr`` so multiple
    tunnels can coexist.
    """

    def __init__(
        self, port: int | None = None, pid: int | None = None
    ) -> None:
        self._port = port
        self._pid = pid
        self._api_addr: str | None = None

    def start(self, port: int, *, domain: str = "") -> str:
        if not shutil.which("ngrok"):
            raise RuntimeError("ngrok binary not found on PATH.")

        from .config import get_tunnel_config

        cfg = get_tunnel_config("ngrok")
        authtoken = os.environ.get("NGROK_AUTHTOKEN", "") or cfg.get("authtoken", "")
        domains: list[str] = cfg.get("domains", [])
        region = cfg.get("region", "")

        self._port = port

        # Use explicit domain override, or allocate from the pool
        if not domain:
            domain = _allocate_ngrok_domain(domains, port)

        cmd: list[str] = ["ngrok", "http", str(port)]
        if domain:
            cmd.extend(["--url", domain])
        if region:
            cmd.extend(["--region", region])

        # Pass authtoken via env var (not CLI arg) to avoid process-list exposure
        env = os.environ.copy()
        if authtoken:
            env["NGROK_AUTHTOKEN"] = authtoken

        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "env": env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **kwargs)
        self._pid = proc.pid

        # If we know the domain, the URL is deterministic — no need to poll
        if domain:
            url = f"https://{domain}"
        else:
            url = self._poll_ngrok_api("127.0.0.1:4040")

        _write_tunnel_state(
            port, proc.pid, url, "ngrok", domain=domain or "",
        )
        return url

    def stop(self) -> None:
        if self._port is not None:
            state = _read_tunnel_state(self._port)
            if state:
                pid = state["pid"]
                if is_process_alive(pid):
                    kill_process_tree(pid)
                _remove_tunnel_state(self._port)
        elif self._pid is not None and is_process_alive(self._pid):
            kill_process_tree(self._pid)

    def get_url(self) -> str | None:
        if self._port is None:
            return None
        state = _read_tunnel_state(self._port)
        if state:
            return state.get("url")
        # Fallback: try the live API
        api_addr = self._api_addr or "127.0.0.1:4040"
        try:
            return self._fetch_ngrok_url(api_addr)
        except Exception:
            return None

    @staticmethod
    def _poll_ngrok_api(api_addr: str, timeout_s: float = 15.0) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                url = NgrokTunnel._fetch_ngrok_url(api_addr)
                if url:
                    return url
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(
            "Timed out waiting for ngrok tunnel URL from local API."
        )

    @staticmethod
    def _fetch_ngrok_url(api_addr: str = "127.0.0.1:4040") -> str | None:
        resp = requests.get(f"http://{api_addr}/api/tunnels", timeout=3)
        resp.raise_for_status()
        tunnels = resp.json().get("tunnels", [])
        for t in tunnels:
            public_url = t.get("public_url", "")
            if public_url.startswith("https://"):
                return public_url
        if tunnels:
            return tunnels[0].get("public_url")
        return None


def _allocate_ngrok_domain(domains: list[str], port: int) -> str:
    """Pick the first domain from *domains* not already in use.

    Returns ``""`` if *domains* is empty (random URL mode).
    Raises ``RuntimeError`` if all domains are taken.
    """
    if not domains:
        return ""

    # Collect domains currently claimed by live ngrok tunnels
    in_use: set[str] = set()
    for state in _all_tunnel_states():
        if state.get("provider") != "ngrok":
            continue
        pid = state.get("pid", 0)
        if pid and is_process_alive(pid):
            d = state.get("domain", "")
            if d:
                in_use.add(d)

    for d in domains:
        if d not in in_use:
            return d

    raise RuntimeError(
        f"All {len(domains)} configured ngrok domain(s) are in use. "
        f"Stop an existing tunnel first or add more domains to "
        f"tunnel.ngrok.domains in config."
    )


# ---------------------------------------------------------------------------
# TailscaleTunnel
# ---------------------------------------------------------------------------

def _funnel_allowed() -> bool:
    """Return True if tailscale funnel (public exposure) is enabled in config.

    Funnel exposes the instance to the public internet, so it is opt-in.
    Set ``tunnel.tailscale.allow_funnel = true`` in
    ``~/.comfy-runner/config.json`` to enable.
    """
    from .config import get_tunnel_config

    cfg = get_tunnel_config("tailscale")
    return bool(cfg.get("allow_funnel", False))


class TailscaleTunnel:
    """Tunnel provider using tailscale funnel."""

    def __init__(
        self, port: int | None = None, pid: int | None = None
    ) -> None:
        self._port = port
        self._pid = pid

    def start(self, port: int) -> str:
        if not _find_tailscale():
            raise RuntimeError("tailscale binary not found on PATH.")

        if not _funnel_allowed():
            raise RuntimeError(
                "Tailscale funnel exposes this instance to the public "
                "internet and is disabled by default. Set "
                "tunnel.tailscale.allow_funnel = true in "
                "~/.comfy-runner/config.json to enable."
            )

        self._port = port

        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | _NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True

        # Funnel only supports ports 443, 8443, 10000 — use --bg on default 443
        result = _run_tailscale(["funnel", "--bg", str(port)], timeout=30)

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"tailscale funnel failed: {stderr}")

        hostname = get_tailscale_hostname()
        url = f"https://{hostname}" if hostname else "https://unknown"
        self._port = port
        _write_tunnel_state(port, 0, url, "tailscale")
        return url

    def stop(self) -> None:
        # Funnel uses default port 443 — turn it off via config
        try:
            _run_tailscale(["funnel", "--https=443", "off"])
        except (subprocess.TimeoutExpired, OSError):
            pass

        if self._port is not None:
            _remove_tunnel_state(self._port)

    def get_url(self) -> str | None:
        if self._port is None:
            return None
        state = _read_tunnel_state(self._port)
        if state:
            return state.get("url")
        return None

    @staticmethod
    def _read_funnel_url(
        proc: subprocess.Popen[bytes],
        timeout_s: float = 15.0,
    ) -> str:
        """Read the funnel URL from tailscale's stdout."""
        import select

        deadline = time.monotonic() + timeout_s
        lines: list[str] = []

        while time.monotonic() < deadline:
            if proc.stdout is None:
                break
            # Read available output with a short timeout
            try:
                line = proc.stdout.readline()
            except Exception:
                break
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
                continue

            text = line.decode("utf-8", errors="replace").strip()
            lines.append(text)

            # Tailscale prints the URL as https://...
            if text.startswith("https://"):
                return text
            # Also look for URL embedded in output
            for word in text.split():
                if word.startswith("https://") and ".ts.net" in word:
                    return word

        raise RuntimeError(
            f"Could not read funnel URL from tailscale output. "
            f"Got: {' | '.join(lines[-5:]) if lines else '(no output)'}"
        )


# ---------------------------------------------------------------------------
# Tailscale helpers — hostname detection + serve management
# ---------------------------------------------------------------------------

def _find_tailscale() -> str | None:
    """Locate the tailscale CLI binary, including macOS app bundle path."""
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "darwin":
        mac_path = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if os.path.isfile(mac_path) and os.access(mac_path, os.X_OK):
            return mac_path
    return None


def _run_tailscale(
    args: list[str],
    timeout: int = 10,
) -> subprocess.CompletedProcess[bytes]:
    """Run a tailscale CLI command with platform-appropriate flags."""
    binary = _find_tailscale()
    if not binary:
        raise RuntimeError("tailscale binary not found on PATH.")
    kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout}
    if sys.platform == "win32":
        kwargs["creationflags"] = _NO_WINDOW
    return subprocess.run([binary, *args], **kwargs)


def get_tailscale_hostname() -> str | None:
    """Return the machine's Tailscale FQDN (e.g. 'mybox.tailnet-name.ts.net'), or None."""
    if not _find_tailscale():
        return None
    try:
        result = _run_tailscale(["status", "--json"])
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        dns_name = data.get("Self", {}).get("DNSName", "")
        # DNSName has a trailing dot, strip it
        return dns_name.rstrip(".") or None
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None


def get_tailscale_serve_url(port: int = 9189) -> str | None:
    """Return the HTTPS URL for tailscale serve on the given port, or None."""
    hostname = get_tailscale_hostname()
    if not hostname:
        return None
    return f"https://{hostname}:{port}"


def start_tailscale_serve(
    port: int = 9189,
    send_output: Callable[[str], None] | None = None,
) -> str:
    """Run `tailscale serve --bg <port>` to expose the runner server over tailnet.

    Returns the HTTPS URL. Raises RuntimeError on failure.
    """
    if not _find_tailscale():
        raise RuntimeError("tailscale binary not found on PATH.")

    hostname = get_tailscale_hostname()
    if not hostname:
        raise RuntimeError(
            "Cannot determine Tailscale hostname. "
            "Is Tailscale running and connected?"
        )

    if send_output:
        send_output(f"Setting up tailscale serve for port {port}...\n")

    result = _run_tailscale(["serve", "--bg", f"--https={port}", str(port)], timeout=15)

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"tailscale serve failed: {stderr}")

    url = f"https://{hostname}:{port}"
    register_served_port(port)
    if send_output:
        send_output(f"✓ Runner server exposed at {url}\n")
        send_output(f"  (only accessible to devices on your tailnet)\n")

    return url


def stop_tailscale_serve(
    port: int = 9189,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Run `tailscale serve --https=<port> off` to stop serving."""
    if send_output:
        send_output("Stopping tailscale serve...\n")

    try:
        _run_tailscale(["serve", f"--https={port}", "off"])
    except (subprocess.TimeoutExpired, OSError):
        pass

    unregister_served_port(port)
    if send_output:
        send_output("✓ Tailscale serve stopped.\n")


# ---------------------------------------------------------------------------
# Persistent serve registry — tracks ports we've registered with tailscale
# so we can clean them up on restart after a crash.
# ---------------------------------------------------------------------------

_SERVE_REGISTRY = CONFIG_DIR / "tailscale-serves.json"


def _load_serve_registry() -> set[int]:
    from safe_file import atomic_read
    raw = atomic_read(_SERVE_REGISTRY)
    if raw is None:
        return set()
    try:
        data = json.loads(raw)
        return set(data.get("ports", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_serve_registry(ports: set[int]) -> None:
    from safe_file import atomic_write
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write(_SERVE_REGISTRY, json.dumps({"ports": sorted(ports)}) + "\n")


def register_served_port(port: int) -> None:
    """Add a port to the persistent serve registry."""
    ports = _load_serve_registry()
    ports.add(port)
    _save_serve_registry(ports)


def unregister_served_port(port: int) -> None:
    """Remove a port from the persistent serve registry."""
    ports = _load_serve_registry()
    ports.discard(port)
    _save_serve_registry(ports)


def cleanup_stale_serves(
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Stop all tailscale serves from the registry and clear it.

    Called on server startup to clean up after a previous crash.
    Also removes any stale tunnel state files (from ngrok/funnel).
    """
    ports = _load_serve_registry()
    if ports:
        if send_output:
            send_output(f"Cleaning up {len(ports)} stale tailscale serve(s)...\n")
        for port in ports:
            try:
                _run_tailscale(["serve", f"--https={port}", "off"])
                if send_output:
                    send_output(f"  Stopped serve on port {port}\n")
            except (subprocess.TimeoutExpired, OSError):
                pass
        _save_serve_registry(set())

    # Remove stale tunnel state files (ngrok processes that died, tailscale
    # funnel sessions whose recorded state is no longer accurate).
    #
    # For tailscale funnel we cannot rely on a child PID (funnel is managed by
    # tailscaled), so the state is written with pid=0. Naively wiping pid=0
    # state would silently lose track of funnels that are still publicly
    # exposed (issue #20). Instead, query funnel liveness and apply policy
    # based on whether the operator has opted in to funnels via
    # tunnel.tailscale.allow_funnel.
    funnel_allowed = _funnel_allowed()
    for state in _all_tunnel_states():
        port = state.get("port")
        if not port:
            continue
        pid = state.get("pid", 0)
        provider = state.get("provider", "")

        if pid and is_process_alive(pid):
            continue  # ngrok-style provider with a live child process

        if provider == "tailscale" and pid == 0:
            active = _tailscale_funnel_active(port)
            if active is True:
                if funnel_allowed:
                    # Operator opted in; preserve the URL across restart.
                    continue
                # Orphan funnel discovered while funnels are disallowed —
                # shut it down so the cleanup is consistent.
                if send_output:
                    send_output(
                        f"  Found active tailscale funnel on port {port} but "
                        f"tunnel.tailscale.allow_funnel is false; shutting it down.\n"
                    )
                try:
                    _run_tailscale(["funnel", "--https=443", "off"])
                except (subprocess.TimeoutExpired, OSError):
                    pass
            elif active is None:
                # Could not verify. Best-effort cleanup if funnels aren't
                # allowed; otherwise preserve and warn.
                if funnel_allowed:
                    if send_output:
                        send_output(
                            f"  Could not verify tailscale funnel for port {port}; "
                            f"preserving state file.\n"
                        )
                    continue
                if send_output:
                    send_output(
                        f"  Could not verify tailscale funnel for port {port}; "
                        f"attempting best-effort shutdown.\n"
                    )
                try:
                    _run_tailscale(["funnel", "--https=443", "off"])
                except (subprocess.TimeoutExpired, OSError):
                    pass
            # active is False: nothing to shut down, just remove the stale file.

        _remove_tunnel_state(port)
        if send_output:
            send_output(f"  Removed stale tunnel state for port {port}\n")


def start_tailscale_serve_port(
    port: int,
    send_output: Callable[[str], None] | None = None,
) -> str:
    """Register a single port with tailscale serve (--https=<port>).
    
    Returns the HTTPS URL. Raises RuntimeError on failure.
    """
    if not _find_tailscale():
        raise RuntimeError("tailscale binary not found on PATH.")

    hostname = get_tailscale_hostname()
    if not hostname:
        raise RuntimeError(
            "Cannot determine Tailscale hostname. "
            "Is Tailscale running and connected?"
        )

    if send_output:
        send_output(f"Registering tailscale serve for port {port}...\n")

    result = _run_tailscale(["serve", "--bg", f"--https={port}", str(port)], timeout=15)

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"tailscale serve for port {port} failed: {stderr}")

    url = f"https://{hostname}:{port}"
    register_served_port(port)
    if send_output:
        send_output(f"✓ Port {port} exposed at {url}\n")

    return url


def stop_tailscale_serve_port(
    port: int,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Remove a single port from tailscale serve."""
    if send_output:
        send_output(f"Removing tailscale serve for port {port}...\n")

    try:
        _run_tailscale(["serve", f"--https={port}", "off"])
    except (subprocess.TimeoutExpired, OSError):
        pass

    unregister_served_port(port)
    if send_output:
        send_output(f"✓ Tailscale serve for port {port} stopped.\n")


def _tailscale_funnel_active(port: int) -> bool | None:
    """Check whether tailscale funnel is currently serving ``port``.

    Returns:
        True  — funnel is configured AND public-internet exposure is on for ``port``.
        False — tailscale is reachable but no funnel is active for ``port``.
        None  — the status could not be determined (binary missing, parse error,
                timeout). Callers should treat None as "unknown" and decide policy.
    """
    if not _find_tailscale():
        return None
    try:
        result = _run_tailscale(["serve", "status", "--json"])
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        # `serve status` returns non-zero when no config exists at all,
        # which means no funnel is active either.
        return False
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

    # tailscale's serve config schema: AllowFunnel maps "<host>:<port>" -> bool.
    # The hostport key uses the machine's MagicDNS name.
    allow_funnel = data.get("AllowFunnel") or {}
    suffix = f":{port}"
    for hostport, enabled in allow_funnel.items():
        if isinstance(hostport, str) and hostport.endswith(suffix) and enabled:
            return True
    return False


def get_tailscale_serve_status() -> dict[str, Any]:
    """Check if tailscale serve is currently active. Returns status dict."""
    if not _find_tailscale():
        return {"active": False, "reason": "tailscale not installed"}

    try:
        result = _run_tailscale(["serve", "status", "--json"])
        if result.returncode != 0:
            return {"active": False, "reason": "serve not configured"}
        data = json.loads(result.stdout)
        # If there are any TCP or web handlers, serve is active
        has_handlers = bool(data.get("TCP") or data.get("Web"))
        hostname = get_tailscale_hostname()
        return {
            "active": has_handlers,
            "hostname": hostname,
            "url": f"https://{hostname}" if hostname else None,
            "config": data,
        }
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return {"active": False, "reason": "could not query serve status"}


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type[NgrokTunnel] | type[TailscaleTunnel]] = {
    "ngrok": NgrokTunnel,
    "tailscale": TailscaleTunnel,
}


def _get_provider(
    name: str,
    port: int | None = None,
    pid: int | None = None,
) -> NgrokTunnel | TailscaleTunnel:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise RuntimeError(f"Unknown tunnel provider: {name!r}")
    return cls(port=port, pid=pid)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def start_tunnel(
    name: str,
    provider: str = "ngrok",
    send_output: Callable[[str], None] | None = None,
    domain: str = "",
) -> dict[str, Any]:
    """Start a tunnel for a running installation.

    Reads the installation's running port from status, spawns the tunnel
    provider, and returns the public URL.  When *domain* is given, it
    overrides the automatic domain pool selection (ngrok only).
    """
    status = get_status(name)
    if not status.get("running"):
        raise RuntimeError(
            f"Installation '{name}' is not running. Start it first."
        )

    port = status["port"]

    # Check if there's already a tunnel on this port
    existing = _read_tunnel_state(port)
    if existing and is_process_alive(existing["pid"]):
        raise RuntimeError(
            f"A tunnel is already running on port {port} "
            f"(provider: {existing['provider']}, URL: {existing['url']}). "
            f"Stop it first with 'tunnel stop'."
        )

    if send_output:
        send_output(f"Starting {provider} tunnel for '{name}' on port {port}...\n")

    tunnel = _get_provider(provider)
    url = tunnel.start(port, domain=domain) if domain and isinstance(tunnel, NgrokTunnel) else tunnel.start(port)

    if send_output:
        send_output(f"\n✓ Tunnel active: {url}\n")

    return {"name": name, "port": port, "provider": provider, "url": url}


def stop_tunnel(
    name: str,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Stop the tunnel for an installation."""
    status = get_status(name)
    port = status.get("port")

    if port is None:
        raise RuntimeError(
            f"Installation '{name}' is not running; cannot determine tunnel port."
        )

    state = _read_tunnel_state(port)
    if not state:
        raise RuntimeError(f"No tunnel found for '{name}' on port {port}.")

    provider_name = state.get("provider", "ngrok")

    if send_output:
        send_output(f"Stopping {provider_name} tunnel on port {port}...\n")

    tunnel = _get_provider(provider_name, port=port, pid=state.get("pid"))
    tunnel.stop()

    if send_output:
        send_output(f"✓ Tunnel stopped.\n")


def get_tunnel_url(name: str) -> str | None:
    """Return the current tunnel URL for an installation, or None."""
    status = get_status(name)
    port = status.get("port")
    if port is None:
        return None
    state = _read_tunnel_state(port)
    if not state:
        return None
    # For providers with tracked PIDs, verify the process is still alive
    pid = state.get("pid", 0)
    if pid and not is_process_alive(pid):
        _remove_tunnel_state(port)
        return None
    return state.get("url")
