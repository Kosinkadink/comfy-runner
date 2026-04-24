"""Tests for test CLI commands — test list, test baseline, test report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_runner_cli.cli import main


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


# ---------------------------------------------------------------------------
# test list
# ---------------------------------------------------------------------------

class TestTestList:
    def test_list_json(self, tmp_path, capsys):
        _make_suite(tmp_path)
        main(["--json", "test", "list", "--dir", str(tmp_path)])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert len(out["suites"]) == 1
        assert out["suites"][0]["name"] == "Test Suite"

    def test_list_empty(self, tmp_path, capsys):
        main(["--json", "test", "list", "--dir", str(tmp_path)])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["suites"] == []

    def test_list_rich(self, tmp_path, capsys):
        _make_suite(tmp_path)
        main(["test", "list", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert "Test Suite" in out


# ---------------------------------------------------------------------------
# test baseline
# ---------------------------------------------------------------------------

class TestTestBaseline:
    def test_approve_workflow(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        # Create fake run output
        run_dir = tmp_path / "run_output"
        wf_dir = run_dir / "wf1" / "9"
        wf_dir.mkdir(parents=True)
        (wf_dir / "out.png").write_bytes(b"test output")

        main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
              "--workflow", "wf1"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert "wf1" in out["approved"]
        # Baseline should exist
        assert (suite_dir / "baselines" / "wf1" / "out.png").is_file()

    def test_approve_all(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        wf_dir = run_dir / "wf1" / "9"
        wf_dir.mkdir(parents=True)
        (wf_dir / "out.png").write_bytes(b"output")

        main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
              "--approve-all"])
        out = json.loads(capsys.readouterr().out)
        assert "wf1" in out["approved"]

    def test_no_args_errors(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        run_dir.mkdir()
        with pytest.raises(SystemExit):
            main(["--json", "test", "baseline", str(suite_dir), str(run_dir)])

    def test_missing_run_dir(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        with pytest.raises(SystemExit):
            main(["--json", "test", "baseline", str(suite_dir),
                  str(tmp_path / "nope"), "--approve-all"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False

    def test_skip_missing_workflow(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        run_dir.mkdir()
        main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
              "--approve-all"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert "wf1" in out["skipped"]

    def test_path_traversal_rejected(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        run_dir.mkdir()
        with pytest.raises(SystemExit):
            main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
                  "--workflow", "../../etc/passwd"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "Invalid workflow name" in out["error"]

    def test_dotdot_traversal_rejected(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        run_dir.mkdir()
        with pytest.raises(SystemExit):
            main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
                  "--workflow", ".."])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "Invalid workflow name" in out["error"]

    def test_collision_warning(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        # Two different node_id dirs with the same filename
        for node_id in ("9", "12"):
            d = run_dir / "wf1" / node_id
            d.mkdir(parents=True)
            (d / "out.png").write_bytes(node_id.encode())
        main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
              "--workflow", "wf1"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert "wf1" in out["approved"]
        assert "wf1/out.png" in out["collisions"]

    def test_workflow_and_approve_all_exclusive(self, tmp_path, capsys):
        suite_dir = _make_suite(tmp_path)
        run_dir = tmp_path / "run_output"
        run_dir.mkdir()
        with pytest.raises(SystemExit):
            main(["--json", "test", "baseline", str(suite_dir), str(run_dir),
                  "--workflow", "wf1", "--approve-all"])


# ---------------------------------------------------------------------------
# test report
# ---------------------------------------------------------------------------

class TestTestReport:
    def test_regenerate_from_summary(self, tmp_path, capsys):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "summary.json").write_text(json.dumps({
            "suite": "Test",
            "total": 2,
            "passed": 2,
            "failed": 0,
            "duration": 3.5,
            "results": [],
        }))
        main(["--json", "test", "report", str(run_dir), "--format", "html"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert "html" in out["files"]
        assert (run_dir / "report.html").is_file()

    def test_regenerate_from_report_json(self, tmp_path, capsys):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "report.json").write_text(json.dumps({
            "suite_name": "Test",
            "timestamp": "2025-01-01T00:00:00+00:00",
            "total": 1,
            "passed": 1,
            "failed": 0,
            "duration": 1.0,
            "workflows": [],
        }), encoding="utf-8")
        main(["--json", "test", "report", str(run_dir), "--format", "markdown"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert "markdown" in out["files"]

    def test_missing_data(self, tmp_path, capsys):
        run_dir = tmp_path / "empty_run"
        run_dir.mkdir()
        with pytest.raises(SystemExit):
            main(["--json", "test", "report", str(run_dir)])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False

    def test_malformed_report_json(self, tmp_path, capsys):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        # report.json with suite_name key but missing required fields
        (run_dir / "report.json").write_text(json.dumps({
            "suite_name": "Test",
        }), encoding="utf-8")
        with pytest.raises(SystemExit):
            main(["--json", "test", "report", str(run_dir)])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "Malformed" in out["error"]

    def test_invalid_json_file(self, tmp_path, capsys):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "report.json").write_text("not valid json", encoding="utf-8")
        with pytest.raises(SystemExit):
            main(["--json", "test", "report", str(run_dir)])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "Failed to read" in out["error"]


# ---------------------------------------------------------------------------
# test run (mocked — no real ComfyUI)
# ---------------------------------------------------------------------------

class TestTestRun:
    def test_invalid_suite(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            main(["--json", "test", "run", str(tmp_path / "nonexistent"),
                  "--target", "http://localhost:8188"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
