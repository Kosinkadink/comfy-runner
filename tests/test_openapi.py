"""Tests for comfy_runner_server.openapi — build_spec smoke test."""

from __future__ import annotations

from comfy_runner_server.openapi import build_spec


class TestBuildSpec:
    def test_returns_valid_structure(self):
        spec = build_spec()
        assert spec["openapi"] == "3.0.3"
        assert "info" in spec
        assert "paths" in spec
        assert "tags" in spec

    def test_has_expected_paths(self):
        spec = build_spec()
        paths = spec["paths"]
        assert "/jobs" in paths
        assert "/installations" in paths
        assert "/{name}/deploy" in paths
        assert "/{name}/status" in paths
        assert "/{name}/nodes" in paths
        assert "/{name}/snapshot" in paths

    def test_all_operations_have_required_fields(self):
        spec = build_spec()
        for path, methods in spec["paths"].items():
            for method, op in methods.items():
                assert "summary" in op, f"Missing summary on {method.upper()} {path}"
                assert "description" in op, f"Missing description on {method.upper()} {path}"
                assert "responses" in op, f"Missing responses on {method.upper()} {path}"

    def test_tags_match_operations(self):
        spec = build_spec()
        tag_names = {t["name"] for t in spec["tags"]}
        used_tags = set()
        for methods in spec["paths"].values():
            for op in methods.values():
                used_tags.update(op.get("tags", []))
        assert used_tags == tag_names
