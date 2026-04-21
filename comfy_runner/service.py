"""Systemd service management for comfy-runner server.

Provides helpers to generate, install, and remove a systemd service
that starts comfy-runner on boot with auto-start and tunnel support.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


_SERVICE_NAME = "comfy-runner"


def _service_file_path() -> Path:
    return Path(f"/etc/systemd/system/{_SERVICE_NAME}.service")


def _find_python() -> str:
    """Return the path to the current Python interpreter."""
    return sys.executable


def _find_runner_script() -> str:
    """Return the path to comfy_runner.py in the repo root."""
    return str(Path(__file__).resolve().parent.parent / "comfy_runner.py")


def generate_unit(
    tailscale: bool = True,
    tunnels: bool = True,
    keep_instances: bool = False,
    port: int = 9189,
) -> str:
    """Generate a systemd unit file string."""
    python = _find_python()
    script = _find_runner_script()

    args = ["server"]
    if tailscale:
        args.append("--tailscale")
    if tunnels:
        args.append("--tunnels")
    if keep_instances:
        args.append("--keep-instances")
    if port != 9189:
        args.extend(["--port", str(port)])

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"
    working_dir = str(Path(script).parent)

    return f"""\
[Unit]
Description=comfy-runner control server
After=network.target tailscaled.service

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
ExecStart={python} {script} {' '.join(args)}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""


def install_service(
    tailscale: bool = True,
    tunnels: bool = True,
    keep_instances: bool = False,
    port: int = 9189,
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Generate and install the systemd service file, then enable it."""
    if sys.platform == "win32":
        raise RuntimeError("Systemd services are only supported on Linux.")

    unit = generate_unit(
        tailscale=tailscale,
        tunnels=tunnels,
        keep_instances=keep_instances,
        port=port,
    )

    service_path = _service_file_path()

    if send_output:
        send_output(f"Writing service file to {service_path}...\n")

    # Write via sudo tee
    result = subprocess.run(
        ["sudo", "tee", str(service_path)],
        input=unit, capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to write service file: {result.stderr.strip()}")

    if send_output:
        send_output("Reloading systemd daemon...\n")

    subprocess.run(
        ["sudo", "systemctl", "daemon-reload"],
        capture_output=True, timeout=10,
    )

    if send_output:
        send_output("Enabling service...\n")

    result = subprocess.run(
        ["sudo", "systemctl", "enable", _SERVICE_NAME],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to enable service: {result.stderr.strip()}")

    if send_output:
        send_output(f"✓ Service '{_SERVICE_NAME}' installed and enabled.\n")
        send_output(f"  Start with: sudo systemctl start {_SERVICE_NAME}\n")

    return {
        "service_name": _SERVICE_NAME,
        "service_path": str(service_path),
        "unit": unit,
    }


def uninstall_service(
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Stop, disable, and remove the systemd service."""
    if sys.platform == "win32":
        raise RuntimeError("Systemd services are only supported on Linux.")

    service_path = _service_file_path()

    # Stop if running
    subprocess.run(
        ["sudo", "systemctl", "stop", _SERVICE_NAME],
        capture_output=True, timeout=10,
    )

    # Disable
    subprocess.run(
        ["sudo", "systemctl", "disable", _SERVICE_NAME],
        capture_output=True, timeout=10,
    )

    # Remove file
    if service_path.exists():
        subprocess.run(
            ["sudo", "rm", str(service_path)],
            capture_output=True, timeout=10,
        )

    subprocess.run(
        ["sudo", "systemctl", "daemon-reload"],
        capture_output=True, timeout=10,
    )

    if send_output:
        send_output(f"✓ Service '{_SERVICE_NAME}' removed.\n")

    return {"service_name": _SERVICE_NAME, "removed": True}


def get_service_status(
    send_output: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Query systemd for the service status."""
    if sys.platform == "win32":
        return {"installed": False, "reason": "not linux"}

    service_path = _service_file_path()
    if not service_path.exists():
        return {"installed": False}

    try:
        result = subprocess.run(
            ["systemctl", "is-active", _SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        active = "unknown"

    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", _SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        enabled = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        enabled = "unknown"

    return {
        "installed": True,
        "active": active,
        "enabled": enabled,
        "service_path": str(service_path),
    }
