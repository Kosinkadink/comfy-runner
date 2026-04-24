"""Tests for Phase 5 test API endpoints — /test/run, /test/results, /test/suites."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_RUNNER_ROOT = Path(__file__).resolve().parent.parent
if str(_RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNNER_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_suite(tmp_path: Path, name: str = "suite") -> Path:
    suite_dir = tmp_path / name
    suite_dir.mkdir()
    (suite_dir / "suite.json").write_text(json.dumps({
        "name": "Test Suite",
        "description": "A test suite",
    }))
    wf_dir = suite_dir / "workflows"
    wf_dir.mkdir()
    (wf_dir / "wf1.json").write_text(json.dumps({
        "1": {"class_type": "KSampler", "inputs": {"seed": 0}},
    }))
    return suite_dir


def _make_run_dir(suite_dir: Path, run_id: str = "20250101-000000") -> Path:
    run_dir = suite_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps({
        "suite_name": "Test Suite",
        "timestamp": "2025-01-01T00:00:00+00:00",
        "total": 1,
        "passed": 1,
        "failed": 0,
        "duration": 1.0,
        "workflows": [],
    }), encoding="utf-8")
    (run_dir / "report.html").write_text("<html></html>", encoding="utf-8")
    return run_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(tmp_config_dir):
    from comfy_runner_server.server import create_app
    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# POST /test/run
# ---------------------------------------------------------------------------

class TestTestRun:
    def test_missing_suite(self, client):
        resp = client.post("/test/run", json={})
        data = resp.get_json()
        assert resp.status_code == 400
        assert data["ok"] is False
        assert "suite" in data["error"].lower()

    def test_missing_installation(self, client):
        resp = client.post("/test/run", json={"suite": "/some/path", "name": "nope"})
        data = resp.get_json()
        assert resp.status_code == 404
        assert data["ok"] is False

    def test_installation_not_running(self, client, tmp_config_dir, monkeypatch):
        from comfy_runner.config import set_installation
        set_installation("main", {"path": "/tmp/fake", "status": "installed"})

        import comfy_runner.process as proc_mod
        monkeypatch.setattr(proc_mod, "get_status", lambda name: {"running": False})

        resp = client.post("/test/run", json={"suite": "/some/path", "name": "main"})
        data = resp.get_json()
        assert resp.status_code == 503
        assert data["ok"] is False
        assert "not running" in data["error"]

    def test_returns_job_id(self, client, tmp_config_dir, monkeypatch, tmp_path):
        from comfy_runner.config import set_installation
        set_installation("main", {"path": "/tmp/fake", "status": "installed"})

        import comfy_runner.process as proc_mod
        monkeypatch.setattr(proc_mod, "get_status", lambda name: {"running": True, "port": 8188})

        suite_dir = _make_suite(tmp_path)
        resp = client.post("/test/run", json={
            "suite": str(suite_dir),
            "name": "main",
        })
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert "job_id" in data
        assert data["async"] is True

    def test_no_installations(self, client, tmp_config_dir):
        resp = client.post("/test/run", json={"suite": "/some/path"})
        data = resp.get_json()
        assert resp.status_code == 400
        assert "no installations" in data["error"].lower()

    def test_invalid_timeout(self, client, tmp_config_dir, monkeypatch):
        from comfy_runner.config import set_installation
        set_installation("main", {"path": "/tmp/fake", "status": "installed"})

        import comfy_runner.process as proc_mod
        monkeypatch.setattr(proc_mod, "get_status", lambda name: {"running": True, "port": 8188})

        resp = client.post("/test/run", json={
            "suite": "/some/path",
            "name": "main",
            "timeout": "bad",
        })
        data = resp.get_json()
        assert resp.status_code == 400
        assert "timeout" in data["error"].lower()

    def test_empty_formats(self, client, tmp_config_dir, monkeypatch):
        from comfy_runner.config import set_installation
        set_installation("main", {"path": "/tmp/fake", "status": "installed"})

        import comfy_runner.process as proc_mod
        monkeypatch.setattr(proc_mod, "get_status", lambda name: {"running": True, "port": 8188})

        resp = client.post("/test/run", json={
            "suite": "/some/path",
            "name": "main",
            "formats": "",
        })
        data = resp.get_json()
        assert resp.status_code == 400
        assert "formats" in data["error"].lower()


# ---------------------------------------------------------------------------
# GET /test/results/<run_id>
# ---------------------------------------------------------------------------

class TestTestResults:
    def test_missing_suite_param(self, client):
        resp = client.get("/test/results/20250101-000000")
        data = resp.get_json()
        assert resp.status_code == 400
        assert "suite" in data["error"].lower()

    def test_path_traversal_backslash_rejected(self, client, tmp_path):
        suite_dir = _make_suite(tmp_path)
        resp = client.get(f"/test/results/..%5Cetc?suite={suite_dir}")
        data = resp.get_json()
        assert resp.status_code == 400
        assert "invalid" in data["error"].lower()

    def test_path_traversal_dotdot_rejected(self, client, tmp_path):
        suite_dir = _make_suite(tmp_path)
        # Path("..").name == ".." — must be explicitly rejected
        (suite_dir / "runs").mkdir()
        resp = client.get(f"/test/results/..?suite={suite_dir}")
        data = resp.get_json()
        assert resp.status_code == 400
        assert "invalid" in data["error"].lower()

    def test_not_found(self, client, tmp_path):
        suite_dir = _make_suite(tmp_path)
        resp = client.get(f"/test/results/99990101-000000?suite={suite_dir}")
        data = resp.get_json()
        assert resp.status_code == 404

    def test_file_listing(self, client, tmp_path):
        suite_dir = _make_suite(tmp_path)
        _make_run_dir(suite_dir)
        resp = client.get(f"/test/results/20250101-000000?suite={suite_dir}")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["run_id"] == "20250101-000000"
        names = [f["name"] for f in data["files"]]
        assert "report.json" in names
        assert "report.html" in names

    def test_json_format(self, client, tmp_path):
        suite_dir = _make_suite(tmp_path)
        _make_run_dir(suite_dir)
        resp = client.get(f"/test/results/20250101-000000?suite={suite_dir}&format=json")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["run_id"] == "20250101-000000"
        assert data["report"]["suite_name"] == "Test Suite"
        assert data["report"]["passed"] == 1


# ---------------------------------------------------------------------------
# GET /test/suites
# ---------------------------------------------------------------------------

class TestTestSuites:
    def test_list_suites(self, client, tmp_path):
        _make_suite(tmp_path)
        resp = client.get(f"/test/suites?dir={tmp_path}")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert len(data["suites"]) == 1
        assert data["suites"][0]["name"] == "Test Suite"

    def test_empty_dir(self, client, tmp_path):
        resp = client.get(f"/test/suites?dir={tmp_path}")
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["ok"] is True
        assert data["suites"] == []


# ---------------------------------------------------------------------------
# OpenAPI spec includes Testing routes
# ---------------------------------------------------------------------------

class TestOpenAPITesting:
    def test_spec_has_test_routes(self, client):
        resp = client.get("/openapi.json")
        data = resp.get_json()
        assert "/test/run" in data["paths"]
        assert "/test/results/{run_id}" in data["paths"]
        assert "/test/suites" in data["paths"]

    def test_testing_tag_exists(self, client):
        resp = client.get("/openapi.json")
        data = resp.get_json()
        tag_names = [t["name"] for t in data["tags"]]
        assert "Testing" in tag_names
