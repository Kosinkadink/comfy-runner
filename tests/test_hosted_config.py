"""Tests for comfy_runner.hosted.config — provider config, volume CRUD, API key fallback."""

from __future__ import annotations

import pytest

from comfy_runner.hosted.config import (
    get_hosted_config,
    get_provider_config,
    get_runpod_api_key,
    get_tailscale_auth_key,
    get_tailscale_oauth_client_id,
    get_tailscale_oauth_client_secret,
    get_volume_config,
    is_tailscale_configured,
    list_volume_configs,
    remove_volume_config,
    set_provider_config,
    set_provider_value,
    set_volume_config,
)


# ---------------------------------------------------------------------------
# Provider config accessors
# ---------------------------------------------------------------------------

class TestProviderConfig:
    def test_get_hosted_config_empty(self, tmp_config_dir):
        assert get_hosted_config() == {}

    def test_get_provider_config_empty(self, tmp_config_dir):
        assert get_provider_config("runpod") == {}

    def test_set_and_get_provider_config(self, tmp_config_dir):
        set_provider_config("runpod", {"api_key": "rk_123", "default_gpu": "A100"})
        cfg = get_provider_config("runpod")
        assert cfg["api_key"] == "rk_123"
        assert cfg["default_gpu"] == "A100"

    def test_set_overwrites_previous(self, tmp_config_dir):
        set_provider_config("runpod", {"api_key": "old"})
        set_provider_config("runpod", {"api_key": "new"})
        assert get_provider_config("runpod")["api_key"] == "new"

    def test_multiple_providers_isolated(self, tmp_config_dir):
        set_provider_config("runpod", {"api_key": "rp"})
        set_provider_config("lambda", {"api_key": "lm"})
        assert get_provider_config("runpod")["api_key"] == "rp"
        assert get_provider_config("lambda")["api_key"] == "lm"


# ---------------------------------------------------------------------------
# set_provider_value — dotted keys, int casting, reserved key protection
# ---------------------------------------------------------------------------

class TestSetProviderValue:
    def test_simple_key(self, tmp_config_dir):
        set_provider_value("runpod", "default_gpu", "A100")
        assert get_provider_config("runpod")["default_gpu"] == "A100"

    def test_dotted_key_creates_nested(self, tmp_config_dir):
        set_provider_value("runpod", "nested.deep.key", "val")
        cfg = get_provider_config("runpod")
        assert cfg["nested"]["deep"]["key"] == "val"

    def test_int_casting_for_cache_releases(self, tmp_config_dir):
        set_provider_value("runpod", "cache_releases", "5")
        assert get_provider_config("runpod")["cache_releases"] == 5

    def test_int_casting_non_numeric_stays_string(self, tmp_config_dir):
        set_provider_value("runpod", "cache_releases", "abc")
        assert get_provider_config("runpod")["cache_releases"] == "abc"

    def test_reserved_key_volumes_raises(self, tmp_config_dir):
        with pytest.raises(ValueError, match="volumes"):
            set_provider_value("runpod", "volumes", "bad")

    def test_reserved_key_nested_volumes_raises(self, tmp_config_dir):
        with pytest.raises(ValueError, match="volumes"):
            set_provider_value("runpod", "some.volumes", "bad")

    def test_non_reserved_key_succeeds(self, tmp_config_dir):
        set_provider_value("runpod", "default_datacenter", "EU-RO-1")
        assert get_provider_config("runpod")["default_datacenter"] == "EU-RO-1"


# ---------------------------------------------------------------------------
# Volume CRUD
# ---------------------------------------------------------------------------

class TestVolumeCRUD:
    def test_set_and_get_volume(self, tmp_config_dir):
        set_volume_config("runpod", "workspace", {"id": "vol_1", "size_gb": 50})
        vol = get_volume_config("runpod", "workspace")
        assert vol is not None
        assert vol["id"] == "vol_1"

    def test_get_nonexistent_volume(self, tmp_config_dir):
        assert get_volume_config("runpod", "nope") is None

    def test_list_volumes_empty(self, tmp_config_dir):
        assert list_volume_configs("runpod") == {}

    def test_list_volumes(self, tmp_config_dir):
        set_volume_config("runpod", "v1", {"id": "a"})
        set_volume_config("runpod", "v2", {"id": "b"})
        vols = list_volume_configs("runpod")
        assert set(vols.keys()) == {"v1", "v2"}

    def test_remove_existing_volume(self, tmp_config_dir):
        set_volume_config("runpod", "rm_me", {"id": "x"})
        assert remove_volume_config("runpod", "rm_me") is True
        assert get_volume_config("runpod", "rm_me") is None

    def test_remove_missing_volume(self, tmp_config_dir):
        assert remove_volume_config("runpod", "nope") is False

    def test_update_existing_volume(self, tmp_config_dir):
        set_volume_config("runpod", "ws", {"id": "v1", "size_gb": 10})
        set_volume_config("runpod", "ws", {"id": "v1", "size_gb": 50})
        assert get_volume_config("runpod", "ws")["size_gb"] == 50


# ---------------------------------------------------------------------------
# get_runpod_api_key — env → config fallback
# ---------------------------------------------------------------------------

class TestRunpodApiKey:
    def test_env_var_takes_precedence(self, tmp_config_dir, monkeypatch):
        set_provider_config("runpod", {"api_key": "config-key"})
        monkeypatch.setenv("RUNPOD_API_KEY", "env-key")
        assert get_runpod_api_key() == "env-key"

    def test_falls_back_to_config(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
        set_provider_config("runpod", {"api_key": "config-key"})
        assert get_runpod_api_key() == "config-key"

    def test_returns_empty_when_neither_set(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
        assert get_runpod_api_key() == ""

    def test_empty_env_var_falls_back(self, tmp_config_dir, monkeypatch):
        monkeypatch.setenv("RUNPOD_API_KEY", "")
        set_provider_config("runpod", {"api_key": "cfg"})
        assert get_runpod_api_key() == "cfg"


# ---------------------------------------------------------------------------
# Tailscale OAuth client credentials — env → config fallback
# ---------------------------------------------------------------------------

class TestTailscaleOAuthCreds:
    def test_client_id_env_var_takes_precedence(self, tmp_config_dir, monkeypatch):
        set_provider_config("runpod", {"tailscale_oauth_client_id": "cfg-id"})
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_ID", "env-id")
        assert get_tailscale_oauth_client_id() == "env-id"

    def test_client_id_falls_back_to_config(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_ID", raising=False)
        set_provider_config("runpod", {"tailscale_oauth_client_id": "cfg-id"})
        assert get_tailscale_oauth_client_id() == "cfg-id"

    def test_client_id_returns_empty_when_neither_set(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_ID", raising=False)
        assert get_tailscale_oauth_client_id() == ""

    def test_client_secret_env_var_takes_precedence(self, tmp_config_dir, monkeypatch):
        set_provider_config("runpod", {"tailscale_oauth_client_secret": "cfg-secret"})
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_SECRET", "env-secret")
        assert get_tailscale_oauth_client_secret() == "env-secret"

    def test_client_secret_falls_back_to_config(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
        set_provider_config("runpod", {"tailscale_oauth_client_secret": "cfg-secret"})
        assert get_tailscale_oauth_client_secret() == "cfg-secret"

    def test_client_secret_returns_empty_when_neither_set(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
        assert get_tailscale_oauth_client_secret() == ""


# ---------------------------------------------------------------------------
# is_tailscale_configured — accepts either OAuth or static auth key
# ---------------------------------------------------------------------------

class TestIsTailscaleConfigured:
    def test_returns_false_when_nothing_set(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("TAILSCALE_AUTH_KEY", raising=False)
        assert is_tailscale_configured() is False

    def test_returns_true_with_static_auth_key(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("TAILSCALE_AUTH_KEY", "tskey-auth-foo")
        assert is_tailscale_configured() is True

    def test_returns_true_with_oauth_pair(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_AUTH_KEY", raising=False)
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_SECRET", "secret")
        assert is_tailscale_configured() is True

    def test_returns_false_with_only_oauth_id(self, tmp_config_dir, monkeypatch):
        # An incomplete OAuth pair must not count as configured.
        monkeypatch.delenv("TAILSCALE_AUTH_KEY", raising=False)
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_ID", "id")
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_SECRET", raising=False)
        assert is_tailscale_configured() is False

    def test_returns_false_with_only_oauth_secret(self, tmp_config_dir, monkeypatch):
        monkeypatch.delenv("TAILSCALE_AUTH_KEY", raising=False)
        monkeypatch.delenv("TAILSCALE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_SECRET", "secret")
        assert is_tailscale_configured() is False

    def test_oauth_pair_takes_precedence_over_static_in_resolution(
        self, tmp_config_dir, monkeypatch,
    ):
        # Both flows present: is_tailscale_configured returns True regardless,
        # but downstream code (runpod_provider.create_pod) prefers OAuth.
        monkeypatch.setenv("TAILSCALE_AUTH_KEY", "tskey-auth-foo")
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("TAILSCALE_OAUTH_CLIENT_SECRET", "secret")
        assert is_tailscale_configured() is True
        # And both individual getters work.
        assert get_tailscale_auth_key() == "tskey-auth-foo"
        assert get_tailscale_oauth_client_id() == "id"
        assert get_tailscale_oauth_client_secret() == "secret"
