"""Tests for comfy_runner.testing.suite — suite loading and discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_runner.testing.suite import Suite, discover_suites, load_suite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_suite(tmp_path: Path, name: str = "test-suite", **meta_kwargs) -> Path:
    """Create a minimal valid test suite directory."""
    suite_dir = tmp_path / name
    suite_dir.mkdir()

    meta = {"name": meta_kwargs.pop("suite_name", "Test Suite"), **meta_kwargs}
    (suite_dir / "suite.json").write_text(json.dumps(meta))

    wf_dir = suite_dir / "workflows"
    wf_dir.mkdir()
    (wf_dir / "basic.json").write_text(json.dumps({"1": {"class_type": "KSampler", "inputs": {}}}))

    return suite_dir


# ---------------------------------------------------------------------------
# load_suite
# ---------------------------------------------------------------------------

class TestLoadSuite:
    def test_loads_valid_suite(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        suite = load_suite(suite_dir)
        assert suite.name == "Test Suite"
        assert len(suite.workflows) == 1
        assert suite.workflows[0].name == "basic.json"

    def test_loads_description(self, tmp_path):
        suite_dir = _make_suite(tmp_path, description="A regression suite")
        suite = load_suite(suite_dir)
        assert suite.description == "A regression suite"

    def test_loads_required_models(self, tmp_path):
        suite_dir = _make_suite(tmp_path, required_models=["model.safetensors"])
        suite = load_suite(suite_dir)
        assert suite.required_models == ["model.safetensors"]

    def test_missing_directory(self, tmp_path):
        with pytest.raises(ValueError, match="not a directory"):
            load_suite(tmp_path / "nonexistent")

    def test_missing_suite_json(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(ValueError, match="Missing suite.json"):
            load_suite(d)

    def test_missing_workflows_dir(self, tmp_path):
        d = tmp_path / "no-wf"
        d.mkdir()
        (d / "suite.json").write_text('{"name": "x"}')
        with pytest.raises(ValueError, match="Missing workflows/"):
            load_suite(d)

    def test_empty_workflows_dir(self, tmp_path):
        d = tmp_path / "empty-wf"
        d.mkdir()
        (d / "suite.json").write_text('{"name": "x"}')
        (d / "workflows").mkdir()
        with pytest.raises(ValueError, match="No workflow JSON"):
            load_suite(d)

    def test_invalid_suite_json(self, tmp_path):
        d = tmp_path / "bad-json"
        d.mkdir()
        (d / "suite.json").write_text("not json")
        with pytest.raises(ValueError, match="Invalid suite.json"):
            load_suite(d)

    def test_loads_config_json(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        config = {"compare": {"default": {"method": "ssim", "threshold": 0.9}}}
        (suite_dir / "config.json").write_text(json.dumps(config))
        suite = load_suite(suite_dir)
        assert suite.config == config

    def test_invalid_config_json(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        (suite_dir / "config.json").write_text("broken")
        with pytest.raises(ValueError, match="Invalid config.json"):
            load_suite(suite_dir)

    def test_multiple_workflows_sorted(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        wf_dir = suite_dir / "workflows"
        (wf_dir / "z_workflow.json").write_text('{"1": {}}')
        (wf_dir / "a_workflow.json").write_text('{"1": {}}')
        suite = load_suite(suite_dir)
        names = [w.stem for w in suite.workflows]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# TestSuite methods
# ---------------------------------------------------------------------------

class TestSuiteMethods:
    def test_has_baseline_false(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        suite = load_suite(suite_dir)
        assert suite.has_baseline("basic") is False

    def test_has_baseline_true(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        bl_dir = suite_dir / "baselines" / "basic"
        bl_dir.mkdir(parents=True)
        (bl_dir / "output_0.png").write_bytes(b"fake")
        suite = load_suite(suite_dir)
        assert suite.has_baseline("basic") is True

    def test_get_baseline_files(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        bl_dir = suite_dir / "baselines" / "basic"
        bl_dir.mkdir(parents=True)
        (bl_dir / "b.png").write_bytes(b"2")
        (bl_dir / "a.png").write_bytes(b"1")
        suite = load_suite(suite_dir)
        files = suite.get_baseline_files("basic")
        assert [f.name for f in files] == ["a.png", "b.png"]

    def test_get_baseline_files_empty(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        suite = load_suite(suite_dir)
        assert suite.get_baseline_files("nonexistent") == []

    def test_get_compare_config_default(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        suite = load_suite(suite_dir)
        cfg = suite.get_compare_config()
        assert cfg == {"method": "existence"}

    def test_get_compare_config_wildcard(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        config = {"compare": {"image/*": {"method": "ssim", "threshold": 0.95}}}
        (suite_dir / "config.json").write_text(json.dumps(config))
        suite = load_suite(suite_dir)
        cfg = suite.get_compare_config("image/png")
        assert cfg == {"method": "ssim", "threshold": 0.95}

    def test_get_compare_config_exact(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        config = {"compare": {"image/png": {"method": "pixel_mse"}, "image/*": {"method": "ssim"}}}
        (suite_dir / "config.json").write_text(json.dumps(config))
        suite = load_suite(suite_dir)
        assert suite.get_compare_config("image/png")["method"] == "pixel_mse"

    def test_get_overrides(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        config = {"overrides": {"seed": 42}}
        (suite_dir / "config.json").write_text(json.dumps(config))
        suite = load_suite(suite_dir)
        assert suite.get_overrides() == {"seed": 42}

    def test_get_overrides_empty(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        suite = load_suite(suite_dir)
        assert suite.get_overrides() == {}


# ---------------------------------------------------------------------------
# discover_suites
# ---------------------------------------------------------------------------

class TestDiscoverSuites:
    def test_finds_suites(self, tmp_path):
        _make_suite(tmp_path, name="suite-a", suite_name="A")
        _make_suite(tmp_path, name="suite-b", suite_name="B")
        suites = discover_suites(tmp_path)
        assert len(suites) == 2
        names = {s.name for s in suites}
        assert names == {"A", "B"}

    def test_skips_invalid(self, tmp_path):
        _make_suite(tmp_path, name="valid", suite_name="Valid")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "suite.json").write_text("not json")
        suites = discover_suites(tmp_path)
        assert len(suites) == 1
        assert suites[0].name == "Valid"

    def test_empty_dir(self, tmp_path):
        assert discover_suites(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert discover_suites(tmp_path / "nope") == []
