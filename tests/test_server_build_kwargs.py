"""Tests for `_extract_build_kwargs` and the deploy schema additions.

Covers the implicit-build behavior on the deploy endpoint:
  * any build-trigger param implies build=true,
  * explicit build=false always wins (suppresses implicit trigger),
  * comfyui_ref is intentionally NOT a trigger (works in both flows),
  * build-trigger params are forwarded only when build mode is active.
"""

from __future__ import annotations

import pytest

from comfy_runner_server.openapi import build_spec
from comfy_runner_server.server import _BUILD_TRIGGER_KEYS, _extract_build_kwargs


class TestExtractBuildKwargs:
    def test_empty_body_returns_empty(self):
        assert _extract_build_kwargs({}) == {}

    def test_no_build_keys_returns_empty(self):
        body = {"branch": "main", "start": True, "comfyui_ref": "v0.3.50"}
        assert _extract_build_kwargs(body) == {}

    def test_explicit_build_true_with_no_extras(self):
        assert _extract_build_kwargs({"build": True}) == {"build": True}

    def test_explicit_build_true_passes_trigger_params(self):
        body = {"build": True, "python_version": "3.12", "gpu": "nvidia"}
        result = _extract_build_kwargs(body)
        assert result == {"build": True, "python_version": "3.12", "gpu": "nvidia"}

    @pytest.mark.parametrize("trigger_key", _BUILD_TRIGGER_KEYS)
    def test_each_trigger_key_implies_build(self, trigger_key):
        body = {trigger_key: "anything"}
        result = _extract_build_kwargs(body)
        assert result["build"] is True
        assert result[trigger_key] == "anything"

    def test_python_version_alone_implies_build(self):
        # The motivating use case for the implicit trigger.
        body = {"python_version": "3.12"}
        assert _extract_build_kwargs(body) == {
            "build": True,
            "python_version": "3.12",
        }

    def test_explicit_false_suppresses_implicit_trigger(self):
        body = {"build": False, "python_version": "3.12"}
        # Explicit no-build wins; trigger params are dropped.
        assert _extract_build_kwargs(body) == {}

    def test_explicit_false_with_no_trigger_returns_empty(self):
        assert _extract_build_kwargs({"build": False}) == {}

    def test_comfyui_ref_does_not_imply_build(self):
        # comfyui_ref is intentionally not a trigger — it works in both flows
        # and is forwarded by the caller separately.
        body = {"comfyui_ref": "v0.3.50"}
        assert _extract_build_kwargs(body) == {}

    def test_only_present_trigger_keys_are_forwarded(self):
        body = {"python_version": "3.12"}
        result = _extract_build_kwargs(body)
        assert result == {"build": True, "python_version": "3.12"}
        # No extra keys leaked through:
        assert set(result) - {"build"} == {"python_version"}


class TestDeployOpenAPISchema:
    def test_deploy_schema_includes_build_params(self):
        spec = build_spec()
        props = (
            spec["paths"]["/{name}/deploy"]["post"]["requestBody"]
            ["content"]["application/json"]["schema"]["properties"]
        )
        # All build-trigger keys plus build itself plus comfyui_ref must be documented
        for key in _BUILD_TRIGGER_KEYS:
            assert key in props, f"Missing {key!r} on deploy schema"
        assert "build" in props
        assert "comfyui_ref" in props

    def test_build_description_mentions_implicit_trigger(self):
        spec = build_spec()
        props = (
            spec["paths"]["/{name}/deploy"]["post"]["requestBody"]
            ["content"]["application/json"]["schema"]["properties"]
        )
        desc = props["build"]["description"].lower()
        assert "implicit" in desc or "implies" in desc

    def test_comfyui_ref_description_disclaims_build_trigger(self):
        spec = build_spec()
        props = (
            spec["paths"]["/{name}/deploy"]["post"]["requestBody"]
            ["content"]["application/json"]["schema"]["properties"]
        )
        desc = props["comfyui_ref"]["description"].lower()
        # Must clearly state comfyui_ref does NOT trigger build mode
        assert "does not imply build" in desc or "does not trigger" in desc.replace("not imply", "not trigger")
