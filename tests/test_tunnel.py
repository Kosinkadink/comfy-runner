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
    _funnel_allowed,
    _get_provider,
    _read_tunnel_state,
    _remove_tunnel_state,
    _tailscale_funnel_active,
    _tunnel_state_path,
    _write_tunnel_state,
    cleanup_stale_serves,
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


# ---------------------------------------------------------------------------
# Funnel opt-in gate
# ---------------------------------------------------------------------------

class TestFunnelAllowed:
    def test_default_false(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.tunnel.get_tunnel_config", lambda _p: {},
            raising=False,
        )
        # Patch where _funnel_allowed actually looks it up
        import comfy_runner.config as cfg
        monkeypatch.setattr(cfg, "get_tunnel_config", lambda _p: {})
        assert _funnel_allowed() is False

    def test_true_when_enabled(self, monkeypatch):
        import comfy_runner.config as cfg
        monkeypatch.setattr(cfg, "get_tunnel_config", lambda _p: {"allow_funnel": True})
        assert _funnel_allowed() is True

    def test_false_when_explicitly_disabled(self, monkeypatch):
        import comfy_runner.config as cfg
        monkeypatch.setattr(cfg, "get_tunnel_config", lambda _p: {"allow_funnel": False})
        assert _funnel_allowed() is False


class TestTailscaleStartGate:
    def test_refuses_without_opt_in(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: False)
        t = TailscaleTunnel()
        with pytest.raises(RuntimeError, match="allow_funnel"):
            t.start(8188)

    def test_allows_when_opted_in(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: True)
        monkeypatch.setattr(
            "comfy_runner.tunnel.get_tailscale_hostname",
            lambda: "host.tailnet-name.ts.net",
        )

        class _FakeResult:
            returncode = 0
            stdout = b""
            stderr = b""

        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale", lambda *a, **k: _FakeResult()
        )
        # Stub the state writer so we don't touch disk.
        written: dict[str, Any] = {}

        def _fake_write(port, pid, url, provider, **extra):
            written.update(port=port, pid=pid, url=url, provider=provider)

        monkeypatch.setattr("comfy_runner.tunnel._write_tunnel_state", _fake_write)

        t = TailscaleTunnel()
        url = t.start(443)
        assert url == "https://host.tailnet-name.ts.net"
        assert written == {
            "port": 443,
            "pid": 0,
            "url": "https://host.tailnet-name.ts.net",
            "provider": "tailscale",
        }


# ---------------------------------------------------------------------------
# _tailscale_funnel_active
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, returncode: int, stdout: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class TestTailscaleFunnelActive:
    def test_returns_none_without_binary(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: None)
        assert _tailscale_funnel_active(443) is None

    def test_returns_true_when_funnel_set(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        payload = json.dumps(
            {"AllowFunnel": {"host.tailnet-name.ts.net:443": True}}
        ).encode("utf-8")
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda *a, **k: _Result(0, payload),
        )
        assert _tailscale_funnel_active(443) is True

    def test_returns_false_when_port_not_funneled(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        payload = json.dumps(
            {"AllowFunnel": {"host.tailnet-name.ts.net:8443": True}}
        ).encode("utf-8")
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda *a, **k: _Result(0, payload),
        )
        assert _tailscale_funnel_active(443) is False

    def test_returns_false_when_funnel_disabled_for_port(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        payload = json.dumps(
            {"AllowFunnel": {"host.tailnet-name.ts.net:443": False}}
        ).encode("utf-8")
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda *a, **k: _Result(0, payload),
        )
        assert _tailscale_funnel_active(443) is False

    def test_returns_false_on_nonzero_exit(self, monkeypatch):
        # serve status returns non-zero when no config is configured at all.
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale", lambda *a, **k: _Result(1, b"")
        )
        assert _tailscale_funnel_active(443) is False

    def test_returns_none_on_malformed_json(self, monkeypatch):
        monkeypatch.setattr("comfy_runner.tunnel._find_tailscale", lambda: "tailscale")
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda *a, **k: _Result(0, b"not json"),
        )
        assert _tailscale_funnel_active(443) is None


# ---------------------------------------------------------------------------
# cleanup_stale_serves — issue #20 regression coverage
# ---------------------------------------------------------------------------

class TestCleanupStaleServes:
    @pytest.fixture
    def cleanup_env(self, tmp_path: Path, monkeypatch):
        """Common scaffolding: tmp config dir + stubbed safe_file + serve registry helpers."""
        monkeypatch.setattr("comfy_runner.tunnel.CONFIG_DIR", tmp_path)

        def _fake_atomic_write(path, content):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")

        def _fake_atomic_read(path):
            p = Path(path)
            return p.read_text(encoding="utf-8") if p.exists() else None

        import sys
        import types
        fake = types.ModuleType("safe_file")
        fake.atomic_write = _fake_atomic_write
        fake.atomic_read = _fake_atomic_read
        monkeypatch.setitem(sys.modules, "safe_file", fake)

        # Empty serve registry — keeps the function focused on the tunnel-state branch.
        monkeypatch.setattr("comfy_runner.tunnel._SERVE_REGISTRY", tmp_path / "tailscale-serves.json")

        return tmp_path

    def _write_state(self, dir_: Path, port: int, **fields):
        data = {"port": port, **fields}
        (dir_ / f"tunnel-{port}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_ngrok_dead_pid_removed(self, cleanup_env, monkeypatch):
        self._write_state(cleanup_env, 8188, pid=999, url="https://x.ngrok", provider="ngrok")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: False)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: False)

        cleanup_stale_serves()
        assert not (cleanup_env / "tunnel-8188.json").exists()

    def test_ngrok_live_pid_kept(self, cleanup_env, monkeypatch):
        self._write_state(cleanup_env, 8188, pid=999, url="https://x.ngrok", provider="ngrok")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: True)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: False)

        cleanup_stale_serves()
        assert (cleanup_env / "tunnel-8188.json").exists()

    def test_active_funnel_with_opt_in_kept(self, cleanup_env, monkeypatch):
        """Regression test for issue #20."""
        self._write_state(cleanup_env, 443, pid=0, url="https://h.ts.net", provider="tailscale")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: False)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: True)
        monkeypatch.setattr("comfy_runner.tunnel._tailscale_funnel_active", lambda port: True)

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda args, **k: calls.append(args) or _Result(0),
        )

        cleanup_stale_serves()
        assert (cleanup_env / "tunnel-443.json").exists(), "state must survive restart"
        assert calls == [], "must not shut down a funnel the operator opted in to"

    def test_active_funnel_without_opt_in_force_stopped(self, cleanup_env, monkeypatch):
        self._write_state(cleanup_env, 443, pid=0, url="https://h.ts.net", provider="tailscale")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: False)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: False)
        monkeypatch.setattr("comfy_runner.tunnel._tailscale_funnel_active", lambda port: True)

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda args, **k: calls.append(args) or _Result(0),
        )
        out: list[str] = []

        cleanup_stale_serves(send_output=out.append)
        assert not (cleanup_env / "tunnel-443.json").exists()
        assert ["funnel", "--https=443", "off"] in calls
        assert any("shutting it down" in line for line in out)

    def test_inactive_funnel_state_removed(self, cleanup_env, monkeypatch):
        self._write_state(cleanup_env, 443, pid=0, url="https://h.ts.net", provider="tailscale")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: False)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: True)
        monkeypatch.setattr("comfy_runner.tunnel._tailscale_funnel_active", lambda port: False)

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda args, **k: calls.append(args) or _Result(0),
        )

        cleanup_stale_serves()
        assert not (cleanup_env / "tunnel-443.json").exists()
        assert calls == [], "inactive funnel needs no shutdown call"

    def test_unknown_status_with_opt_in_kept(self, cleanup_env, monkeypatch):
        self._write_state(cleanup_env, 443, pid=0, url="https://h.ts.net", provider="tailscale")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: False)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: True)
        monkeypatch.setattr("comfy_runner.tunnel._tailscale_funnel_active", lambda port: None)

        out: list[str] = []
        cleanup_stale_serves(send_output=out.append)
        assert (cleanup_env / "tunnel-443.json").exists()
        assert any("Could not verify" in line for line in out)

    def test_unknown_status_without_opt_in_best_effort_off(self, cleanup_env, monkeypatch):
        self._write_state(cleanup_env, 443, pid=0, url="https://h.ts.net", provider="tailscale")
        monkeypatch.setattr("comfy_runner.tunnel.is_process_alive", lambda pid: False)
        monkeypatch.setattr("comfy_runner.tunnel._funnel_allowed", lambda: False)
        monkeypatch.setattr("comfy_runner.tunnel._tailscale_funnel_active", lambda port: None)

        calls: list[list[str]] = []
        monkeypatch.setattr(
            "comfy_runner.tunnel._run_tailscale",
            lambda args, **k: calls.append(args) or _Result(0),
        )
        out: list[str] = []

        cleanup_stale_serves(send_output=out.append)
        assert not (cleanup_env / "tunnel-443.json").exists()
        assert ["funnel", "--https=443", "off"] in calls
        assert any("best-effort" in line for line in out)
