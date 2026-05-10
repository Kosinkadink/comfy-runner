"""Tests for comfy_runner.comfyui — _comfyui_dir, _changed_files,
_parse_porcelain, _is_runtime_ignored, _prepare_clean_tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from comfy_runner.comfyui import (
    _changed_files,
    _comfyui_dir,
    _is_runtime_ignored,
    _parse_porcelain,
    _prepare_clean_tree,
)


class TestComfyuiDir:
    def test_raises_when_missing(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="ComfyUI not found"):
            _comfyui_dir(tmp_path)

    def test_returns_path_when_exists(self, tmp_path: Path):
        (tmp_path / "ComfyUI").mkdir()
        result = _comfyui_dir(tmp_path)
        assert result == tmp_path / "ComfyUI"


class TestChangedFiles:
    def test_empty_when_same_head(self):
        assert _changed_files("/fake", "abc123", "abc123") == []


# ---------------------------------------------------------------------------
# _parse_porcelain
# ---------------------------------------------------------------------------

class TestParsePorcelain:
    def test_empty_input_yields_empty_list(self):
        assert _parse_porcelain("") == []

    def test_modified_and_untracked(self):
        out = " M comfy.py\n?? styles/foo.json\n"
        assert _parse_porcelain(out) == [
            (" M", "comfy.py"),
            ("??", "styles/foo.json"),
        ]

    def test_rename_uses_destination_path(self):
        out = "R  old.py -> new.py\n"
        assert _parse_porcelain(out) == [("R ", "new.py")]

    def test_skips_lines_too_short(self):
        # Real porcelain lines are at least 4 chars ("XY p"); shorter
        # lines indicate a malformed status and are dropped.
        assert _parse_porcelain("ab\n") == []

    def test_staged_then_modified(self):
        out = "MM tracked.py\n"
        assert _parse_porcelain(out) == [("MM", "tracked.py")]


# ---------------------------------------------------------------------------
# _is_runtime_ignored
# ---------------------------------------------------------------------------

class TestIsRuntimeIgnored:
    @pytest.mark.parametrize("path", [
        "styles/foo.json",
        "output/run_001.png",
        "input/upload.png",
        "temp/scratch.bin",
        "user/default/workflows/wf.json",
        "models/checkpoints/sd.safetensors",
    ])
    def test_runtime_paths_are_ignored(self, path: str):
        assert _is_runtime_ignored(path) is True

    @pytest.mark.parametrize("path", [
        "main.py",
        "comfy/cli_args.py",
        "stylesheets/foo.css",  # not 'styles/'
        "outputs.json",          # not 'output/'
    ])
    def test_non_runtime_paths_not_ignored(self, path: str):
        assert _is_runtime_ignored(path) is False

    def test_windows_separators_normalized(self):
        assert _is_runtime_ignored("styles\\foo.json") is True


# ---------------------------------------------------------------------------
# _prepare_clean_tree — integration tests against real git repos
# ---------------------------------------------------------------------------

def _git_available() -> bool:
    return shutil.which("git") is not None


def _init_repo(repo: Path) -> None:
    """Create a single-commit git repo at *repo*."""
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=repo, check=True,
    )
    (repo / "tracked.py").write_text("v1\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True,
        env={**os.environ, **env},
    )


pytestmark_git = pytest.mark.skipif(
    not _git_available(), reason="git binary not on PATH",
)


@pytestmark_git
class TestPrepareCleanTree:
    def test_clean_tree_returns_empty(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        assert _prepare_clean_tree(str(repo)) == {}

    def test_runtime_only_dirty_is_ignored(self, tmp_path: Path):
        """An untracked styles/foo.json must NOT trigger stash/clean."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "styles").mkdir()
        (repo / "styles" / "foo.json").write_text("{}\n")

        captured: list[str] = []
        result = _prepare_clean_tree(
            str(repo), send_output=lambda s: captured.append(s),
        )
        # git collapses a fully untracked dir into a single ``styles/`` entry,
        # but if any tracked sibling existed it would expand to ``styles/foo.json``.
        # Either form must be classified as runtime-ignored.
        assert result.get("ignored_runtime") in (["styles/"], ["styles/foo.json"])
        assert "stashed_sha" not in result
        assert "force_cleaned_paths" not in result
        # The runtime file should still exist on disk.
        assert (repo / "styles" / "foo.json").exists()
        # And there should be no stash entry.
        stash_list = subprocess.run(
            ["git", "stash", "list"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout
        assert stash_list.strip() == ""

    def test_tracked_change_is_stashed(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "tracked.py").write_text("v2-local-edit\n")

        result = _prepare_clean_tree(str(repo))
        assert result.get("stashed_paths") == ["tracked.py"]
        assert result.get("stashed_sha"), "expected a stash sha"
        assert result.get("stash_message", "").startswith("comfy-runner pre-deploy ")
        # File restored to v1 (HEAD content) after stash.
        assert (repo / "tracked.py").read_text() == "v1\n"
        # Stash entry exists and is recoverable.
        stash_list = subprocess.run(
            ["git", "stash", "list"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout
        assert "comfy-runner pre-deploy" in stash_list

    def test_untracked_non_runtime_is_stashed(self, tmp_path: Path):
        """A new file outside the runtime allowlist must be stashed (-u)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "scratch.py").write_text("debug\n")

        result = _prepare_clean_tree(str(repo))
        assert result.get("stashed_paths") == ["scratch.py"]
        # File removed from worktree by stash -u.
        assert not (repo / "scratch.py").exists()

    def test_force_drops_tracked_change(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "tracked.py").write_text("v2-local-edit\n")

        result = _prepare_clean_tree(str(repo), force=True)
        assert result.get("force_cleaned_paths") == ["tracked.py"]
        assert "stashed_sha" not in result
        # File restored to HEAD content; no stash created.
        assert (repo / "tracked.py").read_text() == "v1\n"
        stash_list = subprocess.run(
            ["git", "stash", "list"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout
        assert stash_list.strip() == ""

    def test_force_preserves_runtime_dirs(self, tmp_path: Path):
        """force=true should drop scratch.py but keep styles/foo.json."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "scratch.py").write_text("debug\n")
        (repo / "styles").mkdir()
        (repo / "styles" / "foo.json").write_text("{}\n")
        (repo / "output").mkdir()
        (repo / "output" / "img.png").write_bytes(b"fake")

        result = _prepare_clean_tree(str(repo), force=True)
        assert "scratch.py" in result.get("force_cleaned_paths", [])
        # Runtime files preserved across the clean.
        assert (repo / "styles" / "foo.json").exists()
        assert (repo / "output" / "img.png").exists()
        # Non-runtime untracked file is gone.
        assert not (repo / "scratch.py").exists()

    def test_mixed_runtime_and_real_change(self, tmp_path: Path):
        """Runtime files coexist with a real change → stash only the real."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "styles").mkdir()
        (repo / "styles" / "x.json").write_text("{}\n")
        (repo / "tracked.py").write_text("edit\n")

        result = _prepare_clean_tree(str(repo))
        assert result.get("stashed_paths") == ["tracked.py"]
        assert result.get("ignored_runtime") in (["styles/"], ["styles/x.json"])
        # Runtime file still there even though we stashed the tracked one.
        assert (repo / "styles" / "x.json").exists()
