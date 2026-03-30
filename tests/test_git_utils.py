"""Tests for comfy_runner.git_utils — pure / mockable logic only."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _redact_url
# ---------------------------------------------------------------------------

class TestRedactUrl:
    def test_plain_url_unchanged(self):
        from comfy_runner.git_utils import _redact_url
        url = "https://github.com/Comfy-Org/ComfyUI.git"
        assert _redact_url(url) == url

    def test_strips_user_password(self):
        from comfy_runner.git_utils import _redact_url
        url = "https://x-access-token:ghp_secret@github.com/Comfy-Org/ComfyUI.git"
        result = _redact_url(url)
        assert "ghp_secret" not in result
        assert "x-access-token" not in result
        assert "github.com" in result

    def test_strips_user_only(self):
        from comfy_runner.git_utils import _redact_url
        url = "https://user@github.com/Comfy-Org/ComfyUI.git"
        result = _redact_url(url)
        assert "user@" not in result
        assert "github.com" in result


# ---------------------------------------------------------------------------
# read_git_head — branch ref
# ---------------------------------------------------------------------------

class TestReadGitHead:
    def test_reads_branch_ref(self, tmp_path):
        from comfy_runner.git_utils import read_git_head
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        refs = git_dir / "refs" / "heads"
        refs.mkdir(parents=True)
        sha = "abc123def456" * 3  # 36 chars
        (refs / "main").write_text(sha + "\n")

        assert read_git_head(str(tmp_path)) == sha

    def test_detached_head(self, tmp_path):
        from comfy_runner.git_utils import read_git_head
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        sha = "deadbeef" * 5
        (git_dir / "HEAD").write_text(sha + "\n")
        assert read_git_head(str(tmp_path)) == sha

    def test_packed_refs_fallback(self, tmp_path):
        from comfy_runner.git_utils import read_git_head
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        # No loose ref file — only packed-refs
        sha = "cafebabe12345678" * 2  # 32 chars
        (git_dir / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            f"{sha} refs/heads/main\n"
        )
        assert read_git_head(str(tmp_path)) == sha

    def test_returns_none_for_missing_git(self, tmp_path):
        from comfy_runner.git_utils import read_git_head
        assert read_git_head(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# read_git_remote_url
# ---------------------------------------------------------------------------

class TestReadGitRemoteUrl:
    def test_reads_origin_url(self, tmp_path):
        from comfy_runner.git_utils import read_git_remote_url
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # url must be the first key after [remote "origin"] for the regex
        config = (
            '[core]\n'
            '\trepositoryformatversion = 0\n'
            '[remote "origin"]\n'
            '\turl = https://github.com/Comfy-Org/ComfyUI.git\n'
        )
        (git_dir / "config").write_text(config)
        result = read_git_remote_url(str(tmp_path))
        assert result == "https://github.com/Comfy-Org/ComfyUI.git"

    def test_returns_none_when_no_origin(self, tmp_path):
        from comfy_runner.git_utils import read_git_remote_url
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("[core]\n\tbare = false\n")
        assert read_git_remote_url(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# _resolve_git_dir
# ---------------------------------------------------------------------------

class TestResolveGitDir:
    def test_normal_git_dir(self, tmp_path):
        from comfy_runner.git_utils import _resolve_git_dir
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert _resolve_git_dir(tmp_path) == git_dir

    def test_worktree_git_file(self, tmp_path):
        from comfy_runner.git_utils import _resolve_git_dir
        actual_git = tmp_path / "main-repo" / ".git"
        actual_git.mkdir(parents=True)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {actual_git}\n")
        result = _resolve_git_dir(worktree)
        assert result is not None
        assert result.resolve() == actual_git.resolve()

    def test_returns_none_when_nothing(self, tmp_path):
        from comfy_runner.git_utils import _resolve_git_dir
        assert _resolve_git_dir(tmp_path) is None


# ---------------------------------------------------------------------------
# git_diff_name_only — mock subprocess
# ---------------------------------------------------------------------------

class TestGitDiffNameOnly:
    def test_returns_empty_on_failure(self, monkeypatch):
        from comfy_runner.git_utils import git_diff_name_only
        import subprocess

        def mock_run(*args, **kwargs):
            result = subprocess.CompletedProcess(args=args, returncode=128, stdout="", stderr="")
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        assert git_diff_name_only("/fake/repo", "HEAD~1", "HEAD") == []

    def test_returns_empty_on_exception(self, monkeypatch):
        from comfy_runner.git_utils import git_diff_name_only

        def mock_run(*args, **kwargs):
            raise OSError("git not found")

        monkeypatch.setattr("subprocess.run", mock_run)
        assert git_diff_name_only("/fake/repo", "HEAD~1", "HEAD") == []
