from __future__ import annotations

from pathlib import Path

import pytest

from comfy_runner.config import (
    get_github_token,
    get_installation,
    get_shared_dir,
    get_tunnel_config,
    list_installations,
    load_config,
    remove_installation,
    save_config,
    set_installation,
    set_shared_dir,
    set_tunnel_config,
)


class TestLoadSave:
    def test_defaults_when_no_file(self, tmp_config_dir):
        cfg = load_config()
        assert isinstance(cfg, dict)
        assert cfg["installations"] == {}
        assert cfg["tunnel"] == {}

    def test_round_trip(self, tmp_config_dir):
        cfg = load_config()
        cfg["custom_key"] = 42
        save_config(cfg)

        reloaded = load_config()
        assert reloaded["custom_key"] == 42
        assert reloaded["installations"] == {}


class TestInstallations:
    def test_set_and_get(self, tmp_config_dir):
        set_installation("test1", {"status": "installed", "path": "/tmp/test1"})
        rec = get_installation("test1")
        assert rec is not None
        assert rec["status"] == "installed"

    def test_get_missing(self, tmp_config_dir):
        assert get_installation("nonexistent") is None

    def test_remove(self, tmp_config_dir):
        set_installation("rm_me", {"status": "installed"})
        assert remove_installation("rm_me") is True
        assert get_installation("rm_me") is None

    def test_remove_missing(self, tmp_config_dir):
        assert remove_installation("nope") is False

    def test_list(self, tmp_config_dir):
        set_installation("a", {"status": "installed"})
        set_installation("b", {"status": "pending"})
        result = list_installations()
        assert set(result.keys()) == {"a", "b"}


class TestTunnelConfig:
    def test_get_empty(self, tmp_config_dir):
        assert get_tunnel_config("ngrok") == {}

    def test_set_and_get(self, tmp_config_dir):
        set_tunnel_config("ngrok", {"auth_token": "tok123"})
        assert get_tunnel_config("ngrok") == {"auth_token": "tok123"}


class TestSharedDir:
    def test_default_matches_desktop(self, tmp_config_dir):
        from pathlib import Path
        expected = str(Path.home() / "ComfyUI-Shared")
        assert get_shared_dir() == expected

    def test_set_and_get(self, tmp_config_dir):
        set_shared_dir("/mnt/shared")
        assert get_shared_dir() == "/mnt/shared"

    def test_default_follows_comfy_runner_home(self, tmp_path, monkeypatch):
        """When COMFY_RUNNER_HOME is set, shared dir defaults to a sibling on
        the same filesystem (so on RunPod it lands on /workspace alongside
        /workspace/.comfy-runner rather than on the tiny container rootfs)."""
        import comfy_runner.config as cfg_mod

        runner_home = tmp_path / ".comfy-runner"
        monkeypatch.setenv("COMFY_RUNNER_HOME", str(runner_home))
        monkeypatch.setattr(cfg_mod, "CONFIG_DIR", runner_home)
        monkeypatch.setattr(cfg_mod, "CONFIG_FILE", runner_home / "config.json")

        assert cfg_mod._default_shared_dir() == str(tmp_path / "ComfyUI-Shared")

    def test_default_without_comfy_runner_home(self, monkeypatch):
        """Without COMFY_RUNNER_HOME, default stays at ~/ComfyUI-Shared."""
        from pathlib import Path
        import comfy_runner.config as cfg_mod

        monkeypatch.delenv("COMFY_RUNNER_HOME", raising=False)
        assert cfg_mod._default_shared_dir() == str(Path.home() / "ComfyUI-Shared")

    def test_env_override_wins_over_config(self, tmp_config_dir, monkeypatch):
        """COMFY_RUNNER_SHARED_DIR env var overrides a persisted shared_dir.

        This is the migration path for existing RunPod ci-runner pods
        whose config.json still has shared_dir='/root/ComfyUI-Shared'
        from a boot that predates the COMFY_RUNNER_HOME-aware default.
        """
        # Persist a value that would otherwise win.
        set_shared_dir("/root/ComfyUI-Shared")
        assert get_shared_dir() == "/root/ComfyUI-Shared"

        # Now point the env var at a different location -- it should win.
        monkeypatch.setenv("COMFY_RUNNER_SHARED_DIR", "/workspace/ComfyUI-Shared")
        assert get_shared_dir() == "/workspace/ComfyUI-Shared"

        # Persisted value must not have been modified by the read.
        cfg = load_config()
        assert cfg["shared_dir"] == "/root/ComfyUI-Shared"

    def test_env_override_empty_string_ignored(self, tmp_config_dir, monkeypatch):
        """An empty COMFY_RUNNER_SHARED_DIR is treated as unset."""
        set_shared_dir("/mnt/shared")
        monkeypatch.setenv("COMFY_RUNNER_SHARED_DIR", "")
        assert get_shared_dir() == "/mnt/shared"


class TestGithubToken:
    def test_env_var(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        assert get_github_token() == "env-token"

    def test_falls_back_to_config(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg = load_config()
        cfg["github_token"] = "cfg-token"
        save_config(cfg)
        assert get_github_token() == "cfg-token"
