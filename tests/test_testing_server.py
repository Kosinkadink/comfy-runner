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

    def test_spec_has_ci_runner_routes(self, client):
        resp = client.get("/openapi.json")
        data = resp.get_json()
        assert "/pods/create-ci-runner" in data["paths"]
        assert "/tests/fleet-ci" in data["paths"]
        assert "/tests/{test_id}/artifact/{rel_path}" in data["paths"]


# ---------------------------------------------------------------------------
# GET /tests/<test_id>/artifact/<path>
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_test_runs():
    """Reset the central server's _test_runs dict around each test."""
    from comfy_runner_server import server as srv
    with srv._test_runs_lock:
        snapshot = dict(srv._test_runs)
        srv._test_runs.clear()
    try:
        yield srv
    finally:
        with srv._test_runs_lock:
            srv._test_runs.clear()
            srv._test_runs.update(snapshot)


class TestTestsArtifact:
    """Artifact endpoint serves files relative to a run's output_dir."""

    def _register_run(self, srv, output_dir: Path, test_id: str = "T-test-art-1") -> str:
        """Register a fake completed test run pointing at *output_dir*."""
        srv._test_runs[test_id] = {
            "id": test_id,
            "kind": "single",
            "output_dir": str(output_dir),
            "summary": {},
            "status": "done",
            "created_at": 0,
        }
        return test_id

    def test_serves_file_under_output_dir(self, client, tmp_path, clean_test_runs):
        run_dir = tmp_path / "run"
        wf_dir = run_dir / "wf"
        wf_dir.mkdir(parents=True)
        (wf_dir / "out_0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        test_id = self._register_run(clean_test_runs, run_dir)
        resp = client.get(f"/tests/{test_id}/artifact/wf/out_0.png")
        assert resp.status_code == 200
        assert resp.data.startswith(b"\x89PNG")

    def test_unknown_test_id_404(self, client, tmp_path, clean_test_runs):
        resp = client.get("/tests/T-nope/artifact/x.png")
        assert resp.status_code == 404

    def test_missing_artifact_404(self, client, tmp_path, clean_test_runs):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        test_id = self._register_run(clean_test_runs, run_dir)
        resp = client.get(f"/tests/{test_id}/artifact/missing.png")
        assert resp.status_code == 404

    def test_path_traversal_dotdot_rejected(self, client, tmp_path, clean_test_runs):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (tmp_path / "secret.txt").write_text("nope")
        test_id = self._register_run(clean_test_runs, run_dir)
        resp = client.get(f"/tests/{test_id}/artifact/..%2Fsecret.txt")
        # The route should reject this with 400 (unsafe path component).
        assert resp.status_code in (400, 404)
        if resp.status_code == 400:
            assert resp.get_json()["ok"] is False

    def test_absolute_path_rejected(self, client, tmp_path, clean_test_runs):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        test_id = self._register_run(clean_test_runs, run_dir)
        # Encoded slashes are decoded by Flask before path matching,
        # so this turns into ``/tests/{id}/artifact//etc/passwd``,
        # which Werkzeug normalizes (308 redirect) before it can ever
        # reach a real filesystem path. Either way -- redirect, 400,
        # or 404 -- the request never serves /etc/passwd.
        resp = client.get(f"/tests/{test_id}/artifact/%2Fetc%2Fpasswd")
        assert resp.status_code in (308, 400, 404)
        if resp.status_code == 308:
            # Make sure the redirect target stays inside the test-run
            # subtree (Werkzeug just collapses double slashes).
            location = resp.headers.get("Location", "")
            assert f"/tests/{test_id}/artifact/" in location


# ---------------------------------------------------------------------------
# GET /tests/<test_id>/report?format=html
# ---------------------------------------------------------------------------

class TestTestsReportHtml:
    """HTML report is rendered on-the-fly with artifact-URL prefix."""

    def _register_run_with_report(
        self, srv, run_dir: Path, *, kind: str = "single",
    ) -> str:
        run_dir.mkdir(parents=True, exist_ok=True)
        if kind == "single":
            (run_dir / "report.json").write_text(json.dumps({
                "suite_name": "smoke",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "duration": 1.0,
                "total": 1,
                "passed": 1,
                "failed": 0,
                "workflows": [{
                    "name": "wf1",
                    "passed": True,
                    "execution_time": 0.5,
                    "output_count": 1,
                    "has_baseline": True,
                    "comparisons": [{
                        "baseline_file": "out_0.png",
                        "test_file": "out_0.png",
                        "result": {
                            "method": "ssim",
                            "passed": False,
                            "score": 0.5,
                            "threshold": 0.95,
                            "diff_artifact": "out_0_ssim_diff.png",
                            "details": {},
                        },
                    }],
                }],
                "target_info": {"name": "pod-x"},
            }), encoding="utf-8")
        test_id = "T-test-html-1"
        srv._test_runs[test_id] = {
            "id": test_id,
            "kind": kind,
            "output_dir": str(run_dir),
            "summary": {},
            "status": "done",
            "created_at": 0,
        }
        return test_id

    def test_html_report_contains_artifact_urls(
        self, client, tmp_path, clean_test_runs,
    ):
        run_dir = tmp_path / "run"
        test_id = self._register_run_with_report(clean_test_runs, run_dir)
        resp = client.get(f"/tests/{test_id}/report?format=html")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # The image and the diff overlay both reference the artifact
        # endpoint so the browser actually fetches them.
        assert f"/tests/{test_id}/artifact/wf1/out_0.png" in html
        assert f"/tests/{test_id}/artifact/wf1/out_0_ssim_diff.png" in html
        assert "<img" in html
        # Mimetype is text/html.
        assert resp.mimetype == "text/html"
