"""Tests for comfy_runner.tunnel — state files, domain allocation, provider factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from comfy_runner.tunnel import (
    NgrokTunnel,
    TailscaleTunnel,
    _allocate_ngrok_domain,
    _get_provider,
    _read_tunnel_state,
    _remove_tunnel_state,
    _tunnel_state_path,
    _write_tunnel_state,
)


# ---------------------------------------------------------------------------
# _tunnel_state_path
# ---------------------------------------------------------------------------

class TestTunnelStatePath:
    def test_returns_correct_path(self, monkeypatch):
        fake_dir = Path("/fake/config")
        monkeypatch.setattr("comfy_runner.tunnel.CONFIG_DIR", fake_dir)
        result = _tunnel_state_path(8188)
        assert result == fake_dir / "tunnel-8188.json"


# ---------------------------------------------------------------------------
# State file round-trip
# ---------------------------------------------------------------------------

class TestTunnelStateRoundTrip:
    def test_write_read_remove(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel.CONFIG_DIR", tmp_path)

        # Mock atomic_write to just write directly
        def _fake_atomic_write(path, content):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")

        monkeypatch.setattr("comfy_runner.tunnel.atomic_write", _fake_atomic_write, raising=False)

        # We need to mock the import inside _write_tunnel_state
        import comfy_runner.tunnel as tunnel_mod

        # Patch safe_file.atomic_write at the module level
        import sys
        import types
        fake_safe_file = types.ModuleType("safe_file")
        fake_safe_file.atomic_write = _fake_atomic_write
        monkeypatch.setitem(sys.modules, "safe_file", fake_safe_file)

        _write_tunnel_state(8188, pid=999, url="https://example.ngrok.io", provider="ngrok")

        data = _read_tunnel_state(8188)
        assert data is not None
        assert data["pid"] == 999
        assert data["url"] == "https://example.ngrok.io"
        assert data["provider"] == "ngrok"
        assert data["port"] == 8188

        _remove_tunnel_state(8188)
        assert _read_tunnel_state(8188) is None

    def test_read_missing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel.CONFIG_DIR", tmp_path)
        assert _read_tunnel_state(12345) is None


# ---------------------------------------------------------------------------
# _allocate_ngrok_domain
# ---------------------------------------------------------------------------

class TestAllocateNgrokDomain:
    def test_empty_domains_returns_empty(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._all_tunnel_states", lambda: [])
        assert _allocate_ngrok_domain([], 8188) == ""

    def test_picks_first_unused(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._all_tunnel_states", lambda: [])
        result = _allocate_ngrok_domain(["d1.ngrok.app", "d2.ngrok.app"], 8188)
        assert result == "d1.ngrok.app"

    def test_skips_used_domain(self, monkeypatch):
        # d1 is in use by a live process
        states = [{"provider": "ngrok", "pid": 1, "domain": "d1.ngrok.app"}]
        monkeypatch.setattr("comfy_runner.tunnel._all_tunnel_states", lambda: states)
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: True)
        result = _allocate_ngrok_domain(["d1.ngrok.app", "d2.ngrok.app"], 8188)
        assert result == "d2.ngrok.app"

    def test_raises_when_all_used(self, monkeypatch):
        states = [
            {"provider": "ngrok", "pid": 1, "domain": "d1.ngrok.app"},
            {"provider": "ngrok", "pid": 2, "domain": "d2.ngrok.app"},
        ]
        monkeypatch.setattr("comfy_runner.tunnel._all_tunnel_states", lambda: states)
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: True)
        with pytest.raises(RuntimeError, match="All .* in use"):
            _allocate_ngrok_domain(["d1.ngrok.app", "d2.ngrok.app"], 8188)


# ---------------------------------------------------------------------------
# _get_provider
# ---------------------------------------------------------------------------

class TestGetProvider:
    def test_ngrok(self):
        p = _get_provider("ngrok", port=8000)
        assert isinstance(p, NgrokTunnel)

    def test_tailscale(self):
        p = _get_provider("tailscale", port=8000)
        assert isinstance(p, TailscaleTunnel)

    def test_unknown_raises(self):
        with pytest.raises(RuntimeError, match="Unknown tunnel provider"):
            _get_provider("cloudflare")
