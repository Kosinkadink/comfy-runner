"""Tests for comfy_runner.shared_paths — pure / mockable logic only."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_runner.shared_paths import (
    GENERATED_YAML_NAME,
    KNOWN_MODEL_FOLDERS,
    SHARED_IO_DIRS,
    discover_extra_model_folders,
    ensure_shared_dirs,
    generate_extra_model_paths_yaml,
    get_shared_io_args,
    remove_extra_model_paths,
    sync_custom_model_folders,
    write_extra_model_paths,
)


# ---------------------------------------------------------------------------
# generate_extra_model_paths_yaml
# ---------------------------------------------------------------------------

class TestGenerateExtraModelPathsYaml:
    def test_basic_content(self, tmp_path):
        yaml = generate_extra_model_paths_yaml(tmp_path / "shared")
        assert "comfy_runner_shared:" in yaml
        assert 'is_default: "true"' in yaml
        # Every known folder should appear
        for folder in KNOWN_MODEL_FOLDERS:
            assert f"    {folder}: {folder}/" in yaml

    def test_includes_extra_folders(self, tmp_path):
        yaml = generate_extra_model_paths_yaml(
            tmp_path / "shared", extra_folders=["insightface", "reactor"]
        )
        assert "    insightface: insightface/" in yaml
        assert "    reactor: reactor/" in yaml

    def test_base_path_uses_models_subdir(self, tmp_path):
        shared = tmp_path / "shared"
        yaml = generate_extra_model_paths_yaml(shared)
        # The function converts backslashes to forward slashes for YAML safety
        models_dir = str((shared / "models").resolve()).replace("\\", "/")
        assert f'base_path: "{models_dir}"' in yaml


# ---------------------------------------------------------------------------
# get_shared_io_args
# ---------------------------------------------------------------------------

class TestGetSharedIoArgs:
    def test_returns_args_when_dirs_exist(self, tmp_path):
        (tmp_path / "input").mkdir()
        (tmp_path / "output").mkdir()
        args = get_shared_io_args(tmp_path)
        assert "--input-directory" in args
        assert "--output-directory" in args
        # Values should be absolute resolved paths
        idx_in = args.index("--input-directory") + 1
        idx_out = args.index("--output-directory") + 1
        assert args[idx_in] == str((tmp_path / "input").resolve())
        assert args[idx_out] == str((tmp_path / "output").resolve())

    def test_returns_empty_when_dirs_missing(self, tmp_path):
        assert get_shared_io_args(tmp_path) == []

    def test_partial_dirs(self, tmp_path):
        (tmp_path / "input").mkdir()
        args = get_shared_io_args(tmp_path)
        assert "--input-directory" in args
        assert "--output-directory" not in args


# ---------------------------------------------------------------------------
# ensure_shared_dirs
# ---------------------------------------------------------------------------

class TestEnsureSharedDirs:
    def test_creates_all_dirs(self, tmp_path):
        shared = tmp_path / "shared"
        ensure_shared_dirs(shared)
        # IO dirs
        for d in SHARED_IO_DIRS:
            assert (shared / d).is_dir()
        # Model subdirs
        for folder in KNOWN_MODEL_FOLDERS:
            assert (shared / "models" / folder).is_dir()



# ---------------------------------------------------------------------------
# write_extra_model_paths / remove_extra_model_paths
# ---------------------------------------------------------------------------

class TestWriteAndRemoveExtraModelPaths:
    def test_write_returns_path_and_creates_file(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        shared = tmp_path / "shared"
        result = write_extra_model_paths(install, shared)
        assert result == install / GENERATED_YAML_NAME
        assert result.exists()
        content = result.read_text()
        assert "comfy_runner_shared:" in content

    def test_remove_deletes_file(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        shared = tmp_path / "shared"
        yaml_path = write_extra_model_paths(install, shared)
        assert yaml_path.exists()
        remove_extra_model_paths(install)
        assert not yaml_path.exists()



# ---------------------------------------------------------------------------
# discover_extra_model_folders
# ---------------------------------------------------------------------------

class TestDiscoverExtraModelFolders:
    def test_discovers_non_canonical(self, tmp_path):
        models = tmp_path / "ComfyUI" / "models"
        models.mkdir(parents=True)
        (models / "checkpoints").mkdir()
        (models / "insightface").mkdir()
        (models / "reactor").mkdir()
        extras = discover_extra_model_folders(tmp_path)
        assert "insightface" in extras
        assert "reactor" in extras
        assert "checkpoints" not in extras

    def test_empty_when_only_canonical(self, tmp_path):
        models = tmp_path / "ComfyUI" / "models"
        models.mkdir(parents=True)
        (models / "checkpoints").mkdir()
        (models / "loras").mkdir()
        assert discover_extra_model_folders(tmp_path) == []

    def test_checks_both_layouts(self, tmp_path):
        # Portable layout: models/ directly under install
        models = tmp_path / "models"
        models.mkdir()
        (models / "custom_nodes_models").mkdir()
        extras = discover_extra_model_folders(tmp_path)
        assert "custom_nodes_models" in extras

    def test_excludes_legacy_aliases(self, tmp_path):
        models = tmp_path / "ComfyUI" / "models"
        models.mkdir(parents=True)
        (models / "clip").mkdir()        # legacy alias
        (models / "unet").mkdir()        # legacy alias
        (models / "t2i_adapter").mkdir() # secondary controlnet
        assert discover_extra_model_folders(tmp_path) == []


# ---------------------------------------------------------------------------
# sync_custom_model_folders
# ---------------------------------------------------------------------------

class TestSyncCustomModelFolders:
    def test_discovers_creates_and_reports(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        models = install / "ComfyUI" / "models"
        models.mkdir(parents=True)
        (models / "insightface").mkdir()
        (models / "checkpoints").mkdir()

        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "models").mkdir()

        result = sync_custom_model_folders(install, shared)
        assert "insightface" in result["new_folders"]
        assert "insightface" in result["extra_folders"]
        assert (shared / "models" / "insightface").is_dir()
        assert (shared / "models" / "checkpoints").is_dir()
        assert Path(result["yaml_path"]).exists()

    def test_previous_extras_excluded_from_new(self, tmp_path):
        install = tmp_path / "install"
        install.mkdir()
        models = install / "ComfyUI" / "models"
        models.mkdir(parents=True)
        (models / "insightface").mkdir()
        (models / "reactor").mkdir()

        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "models").mkdir()

        result = sync_custom_model_folders(
            install, shared, previous_extras=["insightface"]
        )
        assert "insightface" not in result["new_folders"]
        assert "reactor" in result["new_folders"]
        assert "insightface" in result["extra_folders"]
