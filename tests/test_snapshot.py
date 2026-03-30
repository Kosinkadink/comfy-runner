"""Tests for comfy_runner.snapshot — timestamp helpers, CRUD, diff, export/import."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from comfy_runner.snapshot import (
    AUTO_SNAPSHOT_LIMIT,
    VALID_TRIGGERS,
    _format_timestamp,
    _is_valid_custom_node,
    _is_valid_snapshot,
    _iso_now,
    _node_to_camel,
    _parse_iso,
    _resolve_snapshot_path,
    _states_match,
    _write_snapshot,
    build_export_envelope,
    delete_snapshot,
    diff_snapshots,
    import_snapshots,
    list_snapshots,
    load_snapshot,
    prune_auto_snapshots,
    resolve_snapshot_id,
    save_snapshot,
    validate_export_envelope,
)


# ---------------------------------------------------------------------------
# Helpers for building fake snapshots
# ---------------------------------------------------------------------------

def _fake_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "comfyui": {
            "ref": "v1.0.0",
            "commit": "abc1234",
            "releaseTag": "v1.0.0",
            "variant": "default",
        },
        "customNodes": [
            {
                "id": "example-node",
                "type": "git",
                "dirName": "example-node",
                "version": "1.0",
                "commit": "aaa1111",
                "enabled": True,
                "url": "https://github.com/example/node.git",
            },
        ],
        "pipPackages": {"numpy": "1.26.0", "torch": "2.1.0"},
    }
    state.update(overrides)
    return state


def _make_snapshot(state: dict[str, Any], trigger: str = "manual", label: str | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "createdAt": _iso_now(),
        "trigger": trigger,
        "label": label,
        **state,
    }


def _write_snapshot_file(snap_dir: Path, snapshot: dict[str, Any], name: str) -> Path:
    snap_dir.mkdir(parents=True, exist_ok=True)
    p = snap_dir / name
    p.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

class TestFormatTimestamp:
    def test_format(self):
        dt = datetime(2026, 3, 17, 6, 57, 21, 729000, tzinfo=timezone.utc)
        assert _format_timestamp(dt) == "20260317_065721_729"

    def test_zero_padded(self):
        dt = datetime(2000, 1, 2, 3, 4, 5, 7000, tzinfo=timezone.utc)
        assert _format_timestamp(dt) == "20000102_030405_007"


class TestIsoNow:
    def test_parseable(self):
        result = _iso_now()
        parsed = _parse_iso(result)
        assert parsed.tzinfo is not None


class TestParseIso:
    def test_z_suffix(self):
        dt = _parse_iso("2026-03-17T06:57:21.729Z")
        assert dt.year == 2026
        assert dt.month == 3

    def test_offset_suffix(self):
        dt = _parse_iso("2026-03-17T06:57:21.729000+00:00")
        assert dt.year == 2026


# ---------------------------------------------------------------------------
# _node_to_camel
# ---------------------------------------------------------------------------

class TestNodeToCamel:
    def test_converts_dir_name(self):
        node = {"id": "x", "type": "git", "dir_name": "my-node"}
        result = _node_to_camel(node)
        assert "dirName" in result
        assert result["dirName"] == "my-node"
        assert "dir_name" not in result

    def test_already_camel(self):
        node = {"id": "x", "type": "git", "dirName": "my-node"}
        result = _node_to_camel(node)
        assert result["dirName"] == "my-node"


# ---------------------------------------------------------------------------
# _resolve_snapshot_path
# ---------------------------------------------------------------------------

class TestResolveSnapshotPath:
    def test_valid_filename(self, tmp_path: Path):
        snap_dir = tmp_path / ".launcher" / "snapshots"
        snap_dir.mkdir(parents=True)
        (snap_dir / "test.json").touch()
        result = _resolve_snapshot_path(tmp_path, "test.json")
        assert result is not None
        assert result.name == "test.json"

    def test_rejects_path_traversal(self, tmp_path: Path):
        assert _resolve_snapshot_path(tmp_path, "../etc/passwd.json") is None

    def test_rejects_non_json(self, tmp_path: Path):
        assert _resolve_snapshot_path(tmp_path, "test.txt") is None

    def test_rejects_empty(self, tmp_path: Path):
        assert _resolve_snapshot_path(tmp_path, "") is None

    def test_rejects_absolute_path(self, tmp_path: Path):
        assert _resolve_snapshot_path(tmp_path, "/tmp/evil.json") is None


# ---------------------------------------------------------------------------
# _is_valid_snapshot / _is_valid_custom_node
# ---------------------------------------------------------------------------

class TestIsValidSnapshot:
    def test_valid(self):
        s = _make_snapshot(_fake_state())
        assert _is_valid_snapshot(s) is True

    def test_bad_version(self):
        s = _make_snapshot(_fake_state())
        s["version"] = 2
        assert _is_valid_snapshot(s) is False

    def test_bad_trigger(self):
        s = _make_snapshot(_fake_state(), trigger="invalid")
        assert _is_valid_snapshot(s) is False

    def test_missing_comfyui(self):
        s = _make_snapshot(_fake_state())
        del s["comfyui"]
        assert _is_valid_snapshot(s) is False

    def test_bad_created_at(self):
        s = _make_snapshot(_fake_state())
        s["createdAt"] = "not-a-date"
        assert _is_valid_snapshot(s) is False


class TestIsValidCustomNode:
    def test_valid_git_node(self):
        n = {"id": "node1", "type": "git", "dirName": "node1"}
        assert _is_valid_custom_node(n) is True

    def test_valid_cnr_node(self):
        n = {"id": "node1", "type": "cnr", "dirName": "node1"}
        assert _is_valid_custom_node(n) is True

    def test_invalid_type(self):
        n = {"id": "node1", "type": "unknown", "dirName": "node1"}
        assert _is_valid_custom_node(n) is False

    def test_missing_id(self):
        n = {"type": "git", "dirName": "node1"}
        assert _is_valid_custom_node(n) is False

    def test_path_traversal_dir(self):
        n = {"id": "node1", "type": "git", "dirName": "../evil"}
        assert _is_valid_custom_node(n) is False

    def test_dot_dir(self):
        n = {"id": "node1", "type": "git", "dirName": "."}
        assert _is_valid_custom_node(n) is False


# ---------------------------------------------------------------------------
# _states_match
# ---------------------------------------------------------------------------

class TestStatesMatch:
    def test_identical(self):
        a = _fake_state()
        b = _fake_state()
        assert _states_match(a, b) is True

    def test_different_commit(self):
        a = _fake_state()
        b = _fake_state(comfyui={**a["comfyui"], "commit": "different"})
        assert _states_match(a, b) is False

    def test_different_pip(self):
        a = _fake_state()
        b = _fake_state(pipPackages={"numpy": "2.0.0"})
        assert _states_match(a, b) is False


# ---------------------------------------------------------------------------
# list_snapshots
# ---------------------------------------------------------------------------

class TestListSnapshots:
    def test_empty_dir(self, tmp_path: Path):
        assert list_snapshots(tmp_path) == []

    def test_newest_first(self, tmp_path: Path):
        snap_dir = tmp_path / ".launcher" / "snapshots"
        snap_dir.mkdir(parents=True)

        old = _make_snapshot(_fake_state(), trigger="boot")
        old["createdAt"] = "2020-01-01T00:00:00.000Z"
        new = _make_snapshot(_fake_state(), trigger="boot")
        new["createdAt"] = "2026-06-01T00:00:00.000Z"

        _write_snapshot_file(snap_dir, old, "old.json")
        _write_snapshot_file(snap_dir, new, "new.json")

        entries = list_snapshots(tmp_path)
        assert len(entries) == 2
        assert entries[0]["filename"] == "new.json"
        assert entries[1]["filename"] == "old.json"


# ---------------------------------------------------------------------------
# save_snapshot / load_snapshot round-trip
# ---------------------------------------------------------------------------

class TestSaveLoadSnapshot:
    def test_roundtrip(self, tmp_path: Path):
        state = _fake_state()
        with patch("comfy_runner.snapshot.capture_state", return_value=state):
            filename = save_snapshot(tmp_path, trigger="manual", label="test")

        loaded = load_snapshot(tmp_path, filename)
        assert loaded["trigger"] == "manual"
        assert loaded["label"] == "test"
        assert loaded["comfyui"]["commit"] == "abc1234"
        assert loaded["pipPackages"]["numpy"] == "1.26.0"


# ---------------------------------------------------------------------------
# delete_snapshot
# ---------------------------------------------------------------------------

class TestDeleteSnapshot:
    def test_delete(self, tmp_path: Path):
        snap_dir = tmp_path / ".launcher" / "snapshots"
        s = _make_snapshot(_fake_state(), trigger="boot")
        _write_snapshot_file(snap_dir, s, "todelete.json")
        assert (snap_dir / "todelete.json").exists()
        delete_snapshot(tmp_path, "todelete.json")
        assert not (snap_dir / "todelete.json").exists()

    def test_delete_invalid_name(self, tmp_path: Path):
        with pytest.raises(ValueError):
            delete_snapshot(tmp_path, "../bad.json")


# ---------------------------------------------------------------------------
# prune_auto_snapshots
# ---------------------------------------------------------------------------

class TestPruneAutoSnapshots:
    def test_prunes_oldest(self, tmp_path: Path):
        snap_dir = tmp_path / ".launcher" / "snapshots"
        snap_dir.mkdir(parents=True)

        for i in range(5):
            s = _make_snapshot(_fake_state(), trigger="boot")
            s["createdAt"] = f"2026-01-0{i+1}T00:00:00.000Z"
            _write_snapshot_file(snap_dir, s, f"snap{i}.json")

        deleted = prune_auto_snapshots(tmp_path, keep=3)
        assert deleted == 2
        remaining = list_snapshots(tmp_path)
        assert len(remaining) == 3

    def test_no_prune_under_limit(self, tmp_path: Path):
        snap_dir = tmp_path / ".launcher" / "snapshots"
        snap_dir.mkdir(parents=True)
        s = _make_snapshot(_fake_state(), trigger="boot")
        _write_snapshot_file(snap_dir, s, "snap0.json")
        deleted = prune_auto_snapshots(tmp_path, keep=10)
        assert deleted == 0


# ---------------------------------------------------------------------------
# resolve_snapshot_id
# ---------------------------------------------------------------------------

class TestResolveSnapshotId:
    def _setup(self, tmp_path: Path):
        snap_dir = tmp_path / ".launcher" / "snapshots"
        snap_dir.mkdir(parents=True)
        for i, name in enumerate(["alpha.json", "beta.json", "gamma.json"]):
            s = _make_snapshot(_fake_state(), trigger="boot")
            s["createdAt"] = f"2026-01-0{i+1}T00:00:00.000Z"
            _write_snapshot_file(snap_dir, s, name)

    def test_direct_filename(self, tmp_path: Path):
        self._setup(tmp_path)
        assert resolve_snapshot_id(tmp_path, "alpha.json") == "alpha.json"

    def test_index(self, tmp_path: Path):
        self._setup(tmp_path)
        # #1 is newest (gamma), #3 is oldest (alpha)
        result = resolve_snapshot_id(tmp_path, "#1")
        assert result == "gamma.json"

    def test_partial_match(self, tmp_path: Path):
        self._setup(tmp_path)
        result = resolve_snapshot_id(tmp_path, "bet")
        assert result == "beta.json"

    def test_ambiguous(self, tmp_path: Path):
        self._setup(tmp_path)
        # "a" matches alpha and gamma
        with pytest.raises(ValueError, match="Ambiguous"):
            resolve_snapshot_id(tmp_path, "a")

    def test_not_found(self, tmp_path: Path):
        self._setup(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            resolve_snapshot_id(tmp_path, "zzz")


# ---------------------------------------------------------------------------
# diff_snapshots
# ---------------------------------------------------------------------------

class TestDiffSnapshots:
    def test_no_changes(self):
        a = _make_snapshot(_fake_state())
        b = _make_snapshot(_fake_state())
        diff = diff_snapshots(a, b)
        assert diff["comfyuiChanged"] is False
        assert diff["nodesAdded"] == []
        assert diff["nodesRemoved"] == []
        assert diff["pipsAdded"] == []
        assert diff["pipsRemoved"] == []

    def test_comfyui_changed(self):
        a = _make_snapshot(_fake_state())
        b_state = _fake_state(comfyui={"ref": "v2.0", "commit": "def5678"})
        b = _make_snapshot(b_state)
        diff = diff_snapshots(a, b)
        assert diff["comfyuiChanged"] is True

    def test_node_added(self):
        a = _make_snapshot(_fake_state(customNodes=[]))
        b = _make_snapshot(_fake_state())
        diff = diff_snapshots(a, b)
        assert len(diff["nodesAdded"]) == 1

    def test_node_removed(self):
        a = _make_snapshot(_fake_state())
        b = _make_snapshot(_fake_state(customNodes=[]))
        diff = diff_snapshots(a, b)
        assert len(diff["nodesRemoved"]) == 1

    def test_pip_added_removed_changed(self):
        a = _make_snapshot(_fake_state(pipPackages={"numpy": "1.0", "old-pkg": "0.1"}))
        b = _make_snapshot(_fake_state(pipPackages={"numpy": "2.0", "new-pkg": "0.5"}))
        diff = diff_snapshots(a, b)
        assert len(diff["pipsAdded"]) == 1
        assert diff["pipsAdded"][0]["name"] == "new-pkg"
        assert len(diff["pipsRemoved"]) == 1
        assert diff["pipsRemoved"][0]["name"] == "old-pkg"
        assert len(diff["pipsChanged"]) == 1
        assert diff["pipsChanged"][0]["name"] == "numpy"


# ---------------------------------------------------------------------------
# validate_export_envelope
# ---------------------------------------------------------------------------

class TestValidateExportEnvelope:
    def test_valid(self):
        envelope = {
            "type": "comfyui-desktop-2-snapshot",
            "version": 1,
            "exportedAt": _iso_now(),
            "installationName": "main",
            "snapshots": [_make_snapshot(_fake_state())],
        }
        result = validate_export_envelope(envelope)
        assert result is envelope

    def test_wrong_type(self):
        envelope = {"type": "wrong", "version": 1, "snapshots": []}
        with pytest.raises(ValueError, match="not a ComfyUI"):
            validate_export_envelope(envelope)

    def test_wrong_version(self):
        envelope = {
            "type": "comfyui-desktop-2-snapshot",
            "version": 99,
            "snapshots": [_make_snapshot(_fake_state())],
        }
        with pytest.raises(ValueError, match="Unsupported"):
            validate_export_envelope(envelope)

    def test_empty_snapshots(self):
        envelope = {
            "type": "comfyui-desktop-2-snapshot",
            "version": 1,
            "snapshots": [],
        }
        with pytest.raises(ValueError, match="no snapshots"):
            validate_export_envelope(envelope)

    def test_not_a_dict(self):
        with pytest.raises(ValueError, match="not a JSON object"):
            validate_export_envelope("string")


# ---------------------------------------------------------------------------
# import_snapshots
# ---------------------------------------------------------------------------

class TestImportSnapshots:
    def test_imports_new(self, tmp_path: Path):
        s = _make_snapshot(_fake_state(), trigger="manual")
        envelope = {"snapshots": [s]}
        result = import_snapshots(tmp_path, envelope)
        assert result["imported"] == 1
        assert result["skipped"] == 0
        assert len(list_snapshots(tmp_path)) == 1

    def test_deduplicates(self, tmp_path: Path):
        s = _make_snapshot(_fake_state(), trigger="manual")
        envelope = {"snapshots": [s]}
        import_snapshots(tmp_path, envelope)
        result = import_snapshots(tmp_path, envelope)
        assert result["imported"] == 0
        assert result["skipped"] == 1
        assert len(list_snapshots(tmp_path)) == 1


# ---------------------------------------------------------------------------
# build_export_envelope
# ---------------------------------------------------------------------------

class TestBuildExportEnvelope:
    def test_structure(self):
        entries = [
            {"filename": "a.json", "snapshot": _make_snapshot(_fake_state())},
        ]
        envelope = build_export_envelope("my-install", entries)
        assert envelope["type"] == "comfyui-desktop-2-snapshot"
        assert envelope["version"] == 1
        assert envelope["installationName"] == "my-install"
        assert len(envelope["snapshots"]) == 1
        assert "exportedAt" in envelope
