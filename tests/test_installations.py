"""Tests for comfy_runner.installations — show_list, remove."""

from __future__ import annotations

from typing import Any

import pytest

from comfy_runner.installations import remove, show_list


class TestShowList:
    def test_empty(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.installations.list_installations", lambda: {}
        )
        assert show_list() == []

    def test_includes_name(self, monkeypatch):
        fake: dict[str, dict[str, Any]] = {
            "main": {"path": "/install/main", "status": "installed"},
            "dev": {"path": "/install/dev", "status": "installed"},
        }
        monkeypatch.setattr(
            "comfy_runner.installations.list_installations", lambda: fake
        )
        result = show_list()
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"main", "dev"}
        for r in result:
            assert "path" in r


class TestRemove:
    def test_raises_for_nonexistent(self, monkeypatch):
        monkeypatch.setattr(
            "comfy_runner.installations.get_installation", lambda name: None
        )
        with pytest.raises(RuntimeError, match="not found"):
            remove("nope")
