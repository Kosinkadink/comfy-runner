"""Tests for comfy_runner.testing.preflight — model pre-flight downloads."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from comfy_runner.testing.preflight import ensure_suite_models
from comfy_runner.testing.suite import load_suite


def _make_suite(tmp_path: Path, models: list[dict] | None = None) -> Path:
    """Create a minimal valid test suite directory with optional models."""
    suite_dir = tmp_path / "test-suite"
    suite_dir.mkdir()

    meta: dict = {"name": "Test Suite"}
    if models is not None:
        meta["models"] = models
    (suite_dir / "suite.json").write_text(json.dumps(meta))

    wf_dir = suite_dir / "workflows"
    wf_dir.mkdir()
    (wf_dir / "basic.json").write_text(json.dumps({"1": {}}))

    return suite_dir


class TestEnsureSuiteModels:
    def test_no_models_is_noop(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path))
        runner = MagicMock()
        result = ensure_suite_models(runner, "main", suite)
        assert result == {"requested": 0, "skipped": 0, "downloaded": 0}
        runner.download_model.assert_not_called()

    def test_skips_existing(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path, models=[
            {"name": "a.safetensors", "directory": "checkpoints",
             "url": "https://example.com/a"},
        ]))
        runner = MagicMock()
        runner.download_model.return_value = {"ok": True, "skipped": True}

        result = ensure_suite_models(runner, "main", suite)

        assert result == {"requested": 1, "skipped": 1, "downloaded": 0}
        runner.download_model.assert_called_once_with(
            "main", url="https://example.com/a", directory="checkpoints",
            filename="a.safetensors", token="",
        )
        runner.poll_job.assert_not_called()

    def test_downloads_missing(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path, models=[
            {"name": "a.safetensors", "directory": "checkpoints",
             "url": "https://example.com/a"},
            {"name": "b.safetensors", "directory": "vae",
             "url": "https://example.com/b"},
        ]))
        runner = MagicMock()
        runner.download_model.side_effect = [
            {"ok": True, "skipped": True},
            {"ok": True, "job_id": "job-b"},
        ]

        result = ensure_suite_models(runner, "main", suite)

        assert result == {"requested": 2, "skipped": 1, "downloaded": 1}
        assert runner.download_model.call_count == 2
        runner.poll_job.assert_called_once()
        assert runner.poll_job.call_args.args[0] == "job-b"

    def test_passes_token(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path, models=[
            {"name": "a.safetensors", "directory": "checkpoints",
             "url": "https://example.com/a", "token": "hf_xxx"},
        ]))
        runner = MagicMock()
        runner.download_model.return_value = {"ok": True, "skipped": True}

        ensure_suite_models(runner, "main", suite)

        runner.download_model.assert_called_once_with(
            "main", url="https://example.com/a", directory="checkpoints",
            filename="a.safetensors", token="hf_xxx",
        )

    def test_raises_when_no_job_id(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path, models=[
            {"name": "a.safetensors", "directory": "checkpoints",
             "url": "https://example.com/a"},
        ]))
        runner = MagicMock()
        runner.download_model.return_value = {"ok": True}  # neither skipped nor job_id

        with pytest.raises(RuntimeError, match="no job_id"):
            ensure_suite_models(runner, "main", suite)

    def test_propagates_install_name(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path, models=[
            {"name": "a.safetensors", "directory": "checkpoints",
             "url": "https://example.com/a"},
        ]))
        runner = MagicMock()
        runner.download_model.return_value = {"ok": True, "skipped": True}

        ensure_suite_models(runner, "experiment", suite)

        assert runner.download_model.call_args.args[0] == "experiment"

    def test_send_output_called(self, tmp_path):
        suite = load_suite(_make_suite(tmp_path, models=[
            {"name": "a.safetensors", "directory": "checkpoints",
             "url": "https://example.com/a"},
        ]))
        runner = MagicMock()
        runner.download_model.return_value = {"ok": True, "skipped": True}
        lines: list[str] = []

        ensure_suite_models(runner, "main", suite, send_output=lines.append)

        joined = "".join(lines)
        assert "Pre-flight" in joined
        assert "checkpoints/a.safetensors" in joined
