"""Tests for CLI hosted commands — redaction, config round-trip, volume arg parsing."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from comfy_runner_cli.cli import _redact_config, main


# ---------------------------------------------------------------------------
# _redact_config
# ---------------------------------------------------------------------------

class TestRedactConfig:
    def test_redacts_api_key(self):
        data = {"api_key": "rk_secret123"}
        result = _redact_config(data)
        assert result["api_key"] == "***"

    def test_redacts_secret_key(self):
        data = {"s3_secret_key": "abc"}
        result = _redact_config(data)
        assert result["s3_secret_key"] == "***"

    def test_redacts_token(self):
        data = {"auth_token": "tok"}
        result = _redact_config(data)
        assert result["auth_token"] == "***"

    def test_redacts_password(self):
        data = {"db_password": "pass123"}
        result = _redact_config(data)
        assert result["db_password"] == "***"

    def test_does_not_redact_normal_keys(self):
        data = {"default_gpu": "A100", "default_datacenter": "US-KS-2"}
        result = _redact_config(data)
        assert result["default_gpu"] == "A100"
        assert result["default_datacenter"] == "US-KS-2"

    def test_redacts_recursively(self):
        data = {
            "runpod": {
                "api_key": "rk_123",
                "s3_secret_key": "sec",
                "default_gpu": "A100",
            }
        }
        result = _redact_config(data)
        assert result["runpod"]["api_key"] == "***"
        assert result["runpod"]["s3_secret_key"] == "***"
        assert result["runpod"]["default_gpu"] == "A100"

    def test_does_not_redact_empty_string(self):
        data = {"api_key": ""}
        result = _redact_config(data)
        assert result["api_key"] == ""

    def test_does_not_mutate_original(self):
        data = {"api_key": "secret"}
        _redact_config(data)
        assert data["api_key"] == "secret"

    def test_case_insensitive_matching(self):
        data = {"API_KEY": "secret", "S3_Secret_Key": "sec"}
        result = _redact_config(data)
        assert result["API_KEY"] == "***"
        assert result["S3_Secret_Key"] == "***"


# ---------------------------------------------------------------------------
# hosted config show / set round-trip
# ---------------------------------------------------------------------------

class TestHostedConfigCLI:
    @patch("comfy_runner.hosted.config.load_config")
    @patch("comfy_runner.hosted.config.save_config")
    def test_config_set_then_show(self, mock_save, mock_load, capsys):
        store = {"installations": {}, "tunnel": {}}
        mock_load.return_value = store
        mock_save.side_effect = lambda cfg: store.update(cfg)

        # Set a value (--json must precede subcommand)
        main(["--json", "hosted", "config", "set", "runpod.default_gpu", "A100"])
        mock_save.assert_called()

        # Now mock the load to return what was saved
        mock_load.return_value = store
        main(["--json", "hosted", "config", "show"])
        out = capsys.readouterr().out
        assert mock_save.call_count >= 1

    @patch("comfy_runner.hosted.config.load_config")
    @patch("comfy_runner.hosted.config.save_config")
    def test_config_set_reserved_key_fails(self, mock_save, mock_load, capsys):
        mock_load.return_value = {"installations": {}, "tunnel": {}}
        with pytest.raises(SystemExit):
            main(["--json", "hosted", "config", "set", "runpod.volumes", "bad"])

    @patch("comfy_runner.hosted.config.load_config")
    @patch("comfy_runner.hosted.config.save_config")
    def test_config_set_missing_provider_prefix(self, mock_save, mock_load, capsys):
        mock_load.return_value = {"installations": {}, "tunnel": {}}
        with pytest.raises(SystemExit):
            main(["hosted", "config", "set", "nogap", "val"])


# ---------------------------------------------------------------------------
# hosted volume create / list / rm argument parsing
# ---------------------------------------------------------------------------

class TestHostedVolumeCLI:
    def test_volume_create_parses_args(self):
        """Verify argparse accepts volume create with required --name and --size."""
        import argparse
        from comfy_runner_cli.cli import main as cli_main

        with patch("comfy_runner.hosted.runpod_provider.RunPodProvider") as MockProv, \
             patch("comfy_runner.hosted.config.load_config") as mock_load, \
             patch("comfy_runner.hosted.config.save_config"):
            mock_load.return_value = {"installations": {}, "tunnel": {}}
            mock_vol = MagicMock()
            mock_vol.id = "vol_new"
            mock_vol.name = "ws"
            mock_vol.datacenter = "US-KS-2"
            mock_vol.size_gb = 50
            MockProv.return_value.create_volume.return_value = mock_vol
            cli_main([
                "--json", "hosted", "volume", "create",
                "--name", "ws", "--size", "50", "--region", "US-KS-2",
            ])

    def test_volume_create_missing_name_fails(self):
        with pytest.raises(SystemExit):
            main(["hosted", "volume", "create", "--size", "50"])

    def test_volume_create_missing_size_fails(self):
        with pytest.raises(SystemExit):
            main(["hosted", "volume", "create", "--name", "ws"])

    def test_volume_list_parses(self):
        with patch("comfy_runner.hosted.config.load_config") as mock_load, \
             patch("comfy_runner.hosted.config.save_config"):
            mock_load.return_value = {"installations": {}, "tunnel": {}}
            main(["--json", "hosted", "volume", "list"])

    def test_volume_rm_requires_name(self):
        with pytest.raises(SystemExit):
            main(["hosted", "volume", "rm"])

    def test_volume_rm_parses_keep_remote(self):
        """Verify --keep-remote flag is accepted."""
        with patch("comfy_runner.hosted.config.load_config") as mock_load, \
             patch("comfy_runner.hosted.config.save_config"), \
             patch("comfy_runner.hosted.config.get_volume_config") as mock_get_vol:
            mock_load.return_value = {"installations": {}, "tunnel": {}}
            mock_get_vol.return_value = {"id": "vol_1"}
            main(["--json", "hosted", "volume", "rm", "ws", "--keep-remote"])
