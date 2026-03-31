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
