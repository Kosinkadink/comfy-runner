"""Shared lifecycle helpers for Tailscale serve and snapshot capture."""

from __future__ import annotations


def maybe_tailscale_serve(port: int, send_output=None) -> None:
    """If Tailscale serve is active (registry non-empty), register this port."""
    from comfy_runner.tunnel import _load_serve_registry, start_tailscale_serve_port
    if not _load_serve_registry():
        return
    try:
        start_tailscale_serve_port(port, send_output=send_output)
    except Exception as e:
        if send_output:
            send_output(f"⚠ Tailscale serve failed: {e}\n")


def maybe_tailscale_unserve(port: int, send_output=None) -> None:
    """If this port is in the Tailscale serve registry, remove it."""
    from comfy_runner.tunnel import _load_serve_registry, stop_tailscale_serve_port
    if port not in _load_serve_registry():
        return
    try:
        stop_tailscale_serve_port(port, send_output=send_output)
    except Exception:
        pass


def capture_snapshot(name: str, trigger: str, send_output=None) -> None:
    """Capture a snapshot and update the installation record (mirrors server behavior)."""
    from comfy_runner.config import get_installation, set_installation
    from comfy_runner.snapshot import capture_snapshot_if_changed, get_snapshot_count

    rec = get_installation(name)
    if not rec:
        return
    last = rec.get("last_snapshot")
    try:
        result = capture_snapshot_if_changed(rec["path"], trigger=trigger, last_snapshot=last)
        if result.get("saved") and result.get("filename"):
            rec = get_installation(name) or rec
            rec["last_snapshot"] = result["filename"]
            rec["snapshot_count"] = get_snapshot_count(rec["path"])
            set_installation(name, rec)
            if send_output:
                send_output(f"Snapshot saved: {result['filename']} (trigger: {trigger})\n")
    except Exception as e:
        if send_output:
            send_output(f"Snapshot capture failed: {e}\n")
