"""Tests for comfy_runner.nodes module."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from comfy_runner.nodes import (
    _is_safe_path_component,
    _safe_extractall,
    _walk_dir,
    disable_node,
    enable_node,
    identify_node,
    node_key,
    remove_node,
    scan_custom_nodes,
)


# ---------------------------------------------------------------------------
# _is_safe_path_component
# ---------------------------------------------------------------------------

class TestIsSafePathComponent:
    @pytest.mark.parametrize("name", ["foo", "my-node", "node_v2", "ComfyUI-Pack"])
    def test_safe_names(self, name: str) -> None:
        assert _is_safe_path_component(name) is True

    @pytest.mark.parametrize("name", ["", ".", "..", "foo/bar", "../etc", "a/b/c"])
    def test_unsafe_names(self, name: str) -> None:
        assert _is_safe_path_component(name) is False


# ---------------------------------------------------------------------------
# node_key
# ---------------------------------------------------------------------------

class TestNodeKey:
    def test_snake_case(self) -> None:
        node = {"type": "cnr", "dir_name": "my_node"}
        assert node_key(node) == "cnr:my_node"

    def test_camel_case(self) -> None:
        node = {"type": "git", "dirName": "myNode"}
        assert node_key(node) == "git:myNode"

    def test_snake_case_takes_precedence(self) -> None:
        node = {"type": "git", "dir_name": "snake", "dirName": "camel"}
        assert node_key(node) == "git:snake"

    def test_missing_dir_name(self) -> None:
        node = {"type": "file"}
        assert node_key(node) == "file:"


# ---------------------------------------------------------------------------
# identify_node
# ---------------------------------------------------------------------------

class TestIdentifyNode:
    def test_cnr_node(self, tmp_path: Path) -> None:
        node_dir = tmp_path / "my-cnr-node"
        node_dir.mkdir()
        (node_dir / ".tracking").write_text("file1.py\nfile2.py\n")
        (node_dir / "pyproject.toml").write_text(
            '[project]\nname = "cool-node"\nversion = "1.2.3"\n'
        )

        result = identify_node(node_dir)
        assert result["type"] == "cnr"
        assert result["id"] == "cool-node"
        assert result["version"] == "1.2.3"
        assert result["dir_name"] == "my-cnr-node"

    def test_cnr_node_no_pyproject(self, tmp_path: Path) -> None:
        node_dir = tmp_path / "my-cnr-node"
        node_dir.mkdir()
        (node_dir / ".tracking").write_text("")

        result = identify_node(node_dir)
        assert result["type"] == "cnr"
        assert result["id"] == "my-cnr-node"
        assert "version" not in result

    def test_git_node(self, tmp_path: Path) -> None:
        node_dir = tmp_path / "my-git-node"
        node_dir.mkdir()
        (node_dir / ".git").mkdir()
        # Mock git operations since we don't have a real repo
        with patch("comfy_runner.nodes.read_git_head", return_value="abc123"), \
             patch("comfy_runner.nodes.read_git_remote_url", return_value="https://github.com/user/repo"):
            result = identify_node(node_dir)

        assert result["type"] == "git"
        assert result["id"] == "my-git-node"
        assert result["commit"] == "abc123"
        assert result["url"] == "https://github.com/user/repo"

    def test_plain_directory_fallback(self, tmp_path: Path) -> None:
        node_dir = tmp_path / "plain-node"
        node_dir.mkdir()

        result = identify_node(node_dir)
        assert result["type"] == "git"
        assert result["id"] == "plain-node"
        assert "commit" not in result


# ---------------------------------------------------------------------------
# scan_custom_nodes
# ---------------------------------------------------------------------------

class TestScanCustomNodes:
    def test_scans_active_and_disabled(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)

        # Active node
        active = cn_dir / "active-node"
        active.mkdir()
        (active / ".tracking").write_text("")

        # Disabled node
        disabled_dir = cn_dir / ".disabled"
        disabled_dir.mkdir()
        disabled = disabled_dir / "disabled-node"
        disabled.mkdir()

        nodes = scan_custom_nodes(tmp_path)
        active_nodes = [n for n in nodes if n["enabled"]]
        disabled_nodes = [n for n in nodes if not n["enabled"]]

        assert len(active_nodes) == 1
        assert active_nodes[0]["dir_name"] == "active-node"
        assert len(disabled_nodes) == 1
        assert disabled_nodes[0]["dir_name"] == "disabled-node"

    def test_skips_dotfiles_and_pycache(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)

        (cn_dir / ".hidden").mkdir()
        (cn_dir / "__pycache__").mkdir()
        (cn_dir / "real-node").mkdir()

        nodes = scan_custom_nodes(tmp_path)
        assert len(nodes) == 1
        assert nodes[0]["dir_name"] == "real-node"

    def test_empty(self, tmp_path: Path) -> None:
        nodes = scan_custom_nodes(tmp_path)
        assert nodes == []


# ---------------------------------------------------------------------------
# enable_node / disable_node
# ---------------------------------------------------------------------------

class TestEnableNode:
    def test_moves_from_disabled_to_active(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        disabled_dir = cn_dir / ".disabled"
        disabled_dir.mkdir(parents=True)
        src = disabled_dir / "my-node"
        src.mkdir()
        (src / "init.py").write_text("# init")

        enable_node(tmp_path, "my-node")

        assert not src.exists()
        assert (cn_dir / "my-node").is_dir()
        assert (cn_dir / "my-node" / "init.py").read_text() == "# init"

    def test_raises_if_not_disabled(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="not found"):
            enable_node(tmp_path, "nonexistent")


class TestDisableNode:
    def test_moves_from_active_to_disabled(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)
        src = cn_dir / "my-node"
        src.mkdir()
        (src / "init.py").write_text("# init")

        disable_node(tmp_path, "my-node")

        assert not src.exists()
        disabled = cn_dir / ".disabled" / "my-node"
        assert disabled.is_dir()
        assert (disabled / "init.py").read_text() == "# init"

    def test_raises_if_not_found(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="not found"):
            disable_node(tmp_path, "nonexistent")


# ---------------------------------------------------------------------------
# remove_node
# ---------------------------------------------------------------------------

class TestRemoveNode:
    def test_removes_active_node(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)
        node = cn_dir / "my-node"
        node.mkdir()
        (node / "file.py").write_text("")

        remove_node(tmp_path, "my-node")
        assert not node.exists()

    def test_removes_disabled_node(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        disabled = cn_dir / ".disabled"
        disabled.mkdir(parents=True)
        node = disabled / "my-node"
        node.mkdir()

        remove_node(tmp_path, "my-node")
        assert not node.exists()

    def test_raises_for_nonexistent(self, tmp_path: Path) -> None:
        cn_dir = tmp_path / "ComfyUI" / "custom_nodes"
        cn_dir.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="not found"):
            remove_node(tmp_path, "ghost-node")


# ---------------------------------------------------------------------------
# _safe_extractall
# ---------------------------------------------------------------------------

class TestSafeExtractall:
    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../etc/passwd", "root:x:0:0")
        buf.seek(0)

        with zipfile.ZipFile(buf, "r") as zf:
            with pytest.raises(RuntimeError, match="path traversal"):
                _safe_extractall(zf, tmp_path)

    def test_allows_normal_entries(self, tmp_path: Path) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("subdir/file.txt", "hello")
        buf.seek(0)

        with zipfile.ZipFile(buf, "r") as zf:
            _safe_extractall(zf, tmp_path)

        assert (tmp_path / "subdir" / "file.txt").read_text() == "hello"


# ---------------------------------------------------------------------------
# _walk_dir
# ---------------------------------------------------------------------------

class TestWalkDir:
    def test_returns_relative_paths(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")

        result = _walk_dir(tmp_path)
        assert "a.py" in result
        assert "sub/b.py" in result

    def test_excludes_tracking_file(self, tmp_path: Path) -> None:
        (tmp_path / ".tracking").write_text("")
        (tmp_path / "real.py").write_text("")

        result = _walk_dir(tmp_path)
        assert ".tracking" not in result
        assert "real.py" in result

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert _walk_dir(tmp_path) == []
