"""Tests for comfy_runner.workflow_models module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from comfy_runner.workflow_models import (
    _format_size,
    check_missing_models,
    cleanup_staging,
    parse_workflow_models,
    resolve_models_dir,
)


# ---------------------------------------------------------------------------
# parse_workflow_models
# ---------------------------------------------------------------------------

class TestParseWorkflowModels:
    def test_extracts_models(self) -> None:
        workflow = {
            "nodes": [
                {
                    "properties": {
                        "models": [
                            {
                                "name": "model.safetensors",
                                "url": "https://example.com/model.safetensors",
                                "directory": "checkpoints",
                            }
                        ]
                    }
                },
                {
                    "properties": {
                        "models": [
                            {
                                "name": "lora.safetensors",
                                "url": "https://example.com/lora.safetensors",
                                "directory": "loras",
                            }
                        ]
                    }
                },
            ]
        }
        result = parse_workflow_models(workflow)
        assert len(result) == 2
        assert result[0]["name"] == "model.safetensors"
        assert result[0]["directory"] == "checkpoints"
        assert result[1]["name"] == "lora.safetensors"

    def test_deduplicates(self) -> None:
        entry = {
            "name": "model.safetensors",
            "url": "https://example.com/model.safetensors",
            "directory": "checkpoints",
        }
        workflow = {
            "nodes": [
                {"properties": {"models": [entry]}},
                {"properties": {"models": [entry]}},
            ]
        }
        result = parse_workflow_models(workflow)
        assert len(result) == 1

    def test_skips_incomplete_entries(self) -> None:
        workflow = {
            "nodes": [
                {
                    "properties": {
                        "models": [
                            {"name": "model.safetensors", "url": "", "directory": "checkpoints"},
                            {"name": "", "url": "https://example.com/x", "directory": "loras"},
                            {"name": "x.bin", "url": "https://example.com/x", "directory": ""},
                            {"name": "good.bin", "url": "https://example.com/good", "directory": "models"},
                        ]
                    }
                }
            ]
        }
        result = parse_workflow_models(workflow)
        assert len(result) == 1
        assert result[0]["name"] == "good.bin"

    def test_empty_workflow(self) -> None:
        assert parse_workflow_models({}) == []
        assert parse_workflow_models({"nodes": []}) == []

    def test_nodes_without_models(self) -> None:
        workflow = {
            "nodes": [
                {"properties": {}},
                {"properties": {"models": "not a list"}},
                {},
            ]
        }
        assert parse_workflow_models(workflow) == []

    def test_recurses_into_subgraphs(self) -> None:
        workflow = {
            "nodes": [
                {
                    "properties": {
                        "models": [
                            {
                                "name": "top.safetensors",
                                "url": "https://example.com/top.safetensors",
                                "directory": "checkpoints",
                            }
                        ]
                    }
                },
            ],
            "definitions": {
                "subgraphs": [
                    {
                        "nodes": [
                            {
                                "properties": {
                                    "models": [
                                        {
                                            "name": "nested.safetensors",
                                            "url": "https://example.com/nested.safetensors",
                                            "directory": "loras",
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "nodes": [
                            {
                                "properties": {
                                    "models": [
                                        {
                                            "name": "other.bin",
                                            "url": "https://example.com/other.bin",
                                            "directory": "vae",
                                        }
                                    ]
                                }
                            }
                        ]
                    },
                ]
            },
        }
        result = parse_workflow_models(workflow)
        names = sorted(m["name"] for m in result)
        assert names == ["nested.safetensors", "other.bin", "top.safetensors"]

    def test_dedupe_across_top_and_subgraphs(self) -> None:
        entry = {
            "name": "shared.safetensors",
            "url": "https://example.com/shared.safetensors",
            "directory": "checkpoints",
        }
        workflow = {
            "nodes": [{"properties": {"models": [entry]}}],
            "definitions": {
                "subgraphs": [
                    {"nodes": [{"properties": {"models": [entry]}}]},
                ]
            },
        }
        result = parse_workflow_models(workflow)
        assert len(result) == 1
        assert result[0]["name"] == "shared.safetensors"

    def test_handles_missing_definitions(self) -> None:
        # Workflow with no 'definitions' key at all should not raise.
        workflow = {"nodes": []}
        assert parse_workflow_models(workflow) == []
        # Workflow with definitions but no subgraphs.
        assert parse_workflow_models({"nodes": [], "definitions": {}}) == []


# ---------------------------------------------------------------------------
# check_missing_models
# ---------------------------------------------------------------------------

class TestCheckMissingModels:
    def test_partitions_missing_and_existing(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        (models_dir / "checkpoints").mkdir(parents=True)
        (models_dir / "checkpoints" / "exists.safetensors").write_bytes(b"\x00")

        models = [
            {"name": "exists.safetensors", "url": "http://x", "directory": "checkpoints"},
            {"name": "missing.safetensors", "url": "http://y", "directory": "checkpoints"},
        ]

        missing, existing = check_missing_models(models, models_dir)
        assert len(existing) == 1
        assert existing[0]["name"] == "exists.safetensors"
        assert len(missing) == 1
        assert missing[0]["name"] == "missing.safetensors"

    def test_all_missing(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        models = [
            {"name": "a.bin", "url": "http://x", "directory": "loras"},
        ]
        missing, existing = check_missing_models(models, models_dir)
        assert len(missing) == 1
        assert len(existing) == 0


# ---------------------------------------------------------------------------
# _format_size
# ---------------------------------------------------------------------------

class TestFormatSize:
    @pytest.mark.parametrize("n,expected", [
        (0, "0 B"),
        (500, "500 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (1572864, "1.5 MB"),
        (1073741824, "1.0 GB"),
        (1610612736, "1.5 GB"),
    ])
    def test_formats(self, n: int, expected: str) -> None:
        assert _format_size(n) == expected


# ---------------------------------------------------------------------------
# cleanup_staging
# ---------------------------------------------------------------------------

class TestCleanupStaging:
    def test_removes_staging_dir(self, tmp_path: Path) -> None:
        staging = tmp_path / ".staging"
        staging.mkdir()
        (staging / "model.part").write_bytes(b"\x00" * 100)
        (staging / "other.part").write_bytes(b"\x00" * 50)

        count = cleanup_staging(tmp_path)
        assert count == 2
        assert not staging.exists()

    def test_noop_without_staging(self, tmp_path: Path) -> None:
        assert cleanup_staging(tmp_path) == 0


# ---------------------------------------------------------------------------
# resolve_models_dir
# ---------------------------------------------------------------------------

class TestResolveModelsDir:
    def test_uses_shared_dir_when_configured(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        with patch("comfy_runner.workflow_models.get_shared_dir", return_value=str(shared)):
            result = resolve_models_dir(tmp_path / "install")
        assert result == (shared / "models").resolve()

    def test_fallback_to_comfyui_models(self, tmp_path: Path) -> None:
        with patch("comfy_runner.workflow_models.get_shared_dir", return_value=""):
            result = resolve_models_dir(tmp_path / "install")
        assert result == tmp_path / "install" / "ComfyUI" / "models"
