"""Tests for comfy_runner.comfyui — _comfyui_dir, _changed_files,
_parse_porcelain_z, _is_runtime_ignored, _prepare_clean_tree.
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
    _parse_porcelain_z,
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
# _parse_porcelain_z
# ---------------------------------------------------------------------------

class TestParsePorcelainZ:
    def test_empty_input_yields_empty_list(self):
        assert _parse_porcelain_z("") == []

    def test_modified_and_untracked(self):
        # -z output: each entry "XY path\0", trailing \0 after last entry.
        out = " M comfy.py\0?? styles/foo.json\0"
        assert _parse_porcelain_z(out) == [
            (" M", ["comfy.py"]),
            ("??", ["styles/foo.json"]),
        ]

    def test_rename_captures_dst_and_src(self):
        # -z rename format: "R  new\0old\0".
        out = "R  new.py\0old.py\0"
        assert _parse_porcelain_z(out) == [("R ", ["new.py", "old.py"])]

    def test_copy_captures_dst_and_src(self):
        out = "C  copy.py\0orig.py\0"
        assert _parse_porcelain_z(out) == [("C ", ["copy.py", "orig.py"])]

    def test_path_with_space_passes_through_verbatim(self):
        # Without -z, git would quote this as ``"my file.txt"``. With -z
        # the path comes through raw and is never wrapped in quotes.
        out = "?? my file.txt\0"
        assert _parse_porcelain_z(out) == [("??", ["my file.txt"])]

    def test_skips_fields_too_short(self):
        assert _parse_porcelain_z("ab\0") == []

    def test_staged_then_modified(self):
        assert _parse_porcelain_z("MM tracked.py\0") == [("MM", ["tracked.py"])]


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
        "custom_nodes/ComfyUI-Manager/__init__.py",
        "custom_nodes/",
    ])
    def test_runtime_paths_are_ignored(self, path: str):
        assert _is_runtime_ignored(path) is True

    @pytest.mark.parametrize("path", [
        "main.py",
        "comfy/cli_args.py",
        "stylesheets/foo.css",     # not 'styles/'
        "outputs.json",            # not 'output/'
        "custom_nodes_helper.py",  # not 'custom_nodes/'
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

    def test_custom_nodes_preserved_under_force(self, tmp_path: Path):
        """force=True must NOT delete user-installed custom nodes."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        # Pretend the user manually cloned a custom node.
        (repo / "custom_nodes" / "ComfyUI-Manager").mkdir(parents=True)
        (repo / "custom_nodes" / "ComfyUI-Manager" / "__init__.py").write_text("# manager\n")
        # Also have a real change so we exercise the force path.
        (repo / "tracked.py").write_text("edit\n")

        result = _prepare_clean_tree(str(repo), force=True)
        assert "tracked.py" in result.get("force_cleaned_paths", [])
        # Custom node directory survives the clean.
        assert (repo / "custom_nodes" / "ComfyUI-Manager" / "__init__.py").exists()

    def test_rename_leaves_clean_tree(self, tmp_path: Path):
        """A staged rename must leave the working tree clean afterwards.

        Pathspec-stash of a rename is fragile (the source path is gone
        from the worktree, so ``git stash push -- src dst`` fails). The
        important invariant for the deploy is just that the subsequent
        ``git checkout`` won't abort — which the hard-clean fallback
        guarantees.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        # git mv stages a rename of the tracked file.
        subprocess.run(
            ["git", "mv", "tracked.py", "renamed.py"],
            cwd=repo, check=True,
        )

        result = _prepare_clean_tree(str(repo))
        # Either path may be reported via stash or via the force-clean
        # fallback — what matters is the tree ends clean.
        touched = (
            result.get("stashed_paths", [])
            + result.get("force_cleaned_paths", [])
        )
        assert "tracked.py" in touched
        assert "renamed.py" in touched
        status_after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert status_after == ""

    def test_path_with_space_is_handled(self, tmp_path: Path):
        """A new untracked file whose name contains a space must stash cleanly.

        Regression test for the old line-based porcelain parser, which
        passed git's C-style ``"my file.txt"`` quoted form straight to
        ``git stash`` and crashed.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "my file.txt").write_text("hi\n")

        result = _prepare_clean_tree(str(repo))
        assert result.get("stashed_paths") == ["my file.txt"]
        assert not (repo / "my file.txt").exists()
        # Stash entry exists.
        stash_list = subprocess.run(
            ["git", "stash", "list"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout
        assert "comfy-runner pre-deploy" in stash_list

    def test_stash_failure_falls_back_to_hard_clean(self, tmp_path: Path, monkeypatch):
        """If git stash fails, we must hard-clean rather than abort the deploy."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "tracked.py").write_text("edit\n")

        import subprocess as real_sp

        from comfy_runner import comfyui as comfyui_mod

        original_run = real_sp.run

        def fake_run(cmd, *args, **kwargs):
            # Make ``git stash push ...`` always fail; let everything
            # else (status, reset, clean, rev-parse, ...) run normally.
            if (
                isinstance(cmd, list)
                and len(cmd) >= 3
                and cmd[0] == "git"
                and cmd[1] == "stash"
                and cmd[2] == "push"
            ):
                raise real_sp.CalledProcessError(
                    1, cmd, output="", stderr="simulated stash failure",
                )
            return original_run(cmd, *args, **kwargs)

        # _prepare_clean_tree imports subprocess as ``_sp`` inside the
        # function body, so patching the top-level module attribute is
        # enough — the import resolves to the same module object.
        monkeypatch.setattr(comfyui_mod, "_sp", real_sp, raising=False)
        monkeypatch.setattr(real_sp, "run", fake_run)

        captured: list[str] = []
        result = _prepare_clean_tree(
            str(repo), send_output=lambda s: captured.append(s),
        )
        # Stash failure recorded but deploy continues.
        assert "stash_failed" in result
        assert "simulated stash failure" in result["stash_failed"]
        # And the hard-clean fallback ran.
        assert result.get("force_cleaned_paths") == ["tracked.py"]
        # Working tree must be clean afterwards.
        status_after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert status_after == ""
        # Operator gets a warning line.
        assert any("falling back to reset+clean" in line for line in captured)

    def test_status_failure_returns_status_error(self, tmp_path: Path, monkeypatch):
        """If git status itself fails, return a status_error and let the caller proceed."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        import subprocess as real_sp

        original_run = real_sp.run

        def fake_run(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list)
                and len(cmd) >= 2
                and cmd[0] == "git"
                and cmd[1] == "status"
            ):
                raise OSError("simulated status crash")
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(real_sp, "run", fake_run)

        result = _prepare_clean_tree(str(repo))
        assert "status_error" in result
        assert "simulated status crash" in result["status_error"]
