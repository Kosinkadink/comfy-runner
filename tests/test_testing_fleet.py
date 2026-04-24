"""Tests for Phase 7 Fleet Orchestration — fleet.py and CLI integration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comfy_runner.testing.fleet import (
    EphemeralTarget,
    FleetResult,
    LocalTarget,
    RemoteTarget,
    TargetResult,
    _make_safe_dirname,
    parse_target_spec,
    render_fleet_console,
    render_fleet_json,
    run_fleet,
)


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


def _mock_report(total: int = 2, passed: int = 2, failed: int = 0) -> MagicMock:
    report = MagicMock()
    report.total = total
    report.passed = passed
    report.failed = failed
    report.duration = 1.5
    report.suite_name = "Test Suite"
    report.to_dict.return_value = {
        "suite_name": "Test Suite",
        "total": total,
        "passed": passed,
        "failed": failed,
        "duration": 1.5,
    }
    return report


# ---------------------------------------------------------------------------
# TargetResult
# ---------------------------------------------------------------------------

class TestTargetResult:
    def test_passed_when_no_failures(self):
        r = TargetResult(
            target_name="local",
            target_kind="local",
            report=_mock_report(total=3, passed=3, failed=0),
        )
        assert r.passed is True

    def test_failed_when_failures(self):
        r = TargetResult(
            target_name="local",
            target_kind="local",
            report=_mock_report(total=3, passed=2, failed=1),
        )
        assert r.passed is False

    def test_failed_when_error(self):
        r = TargetResult(
            target_name="local",
            target_kind="local",
            error="Connection refused",
        )
        assert r.passed is False

    def test_failed_when_no_report(self):
        r = TargetResult(
            target_name="local",
            target_kind="local",
        )
        assert r.passed is False

    def test_to_dict(self):
        r = TargetResult(
            target_name="my-target",
            target_kind="local",
            output_dir=Path("/out"),
            duration=3.14159,
            report=_mock_report(),
        )
        d = r.to_dict()
        assert d["target_name"] == "my-target"
        assert d["target_kind"] == "local"
        assert d["duration"] == 3.14
        assert d["passed"] is True
        assert "report" in d
        assert d["output_dir"] == str(Path("/out"))

    def test_to_dict_with_error(self):
        r = TargetResult(
            target_name="fail",
            target_kind="remote",
            error="boom",
        )
        d = r.to_dict()
        assert d["error"] == "boom"
        assert d["passed"] is False
        assert "report" not in d


# ---------------------------------------------------------------------------
# FleetResult
# ---------------------------------------------------------------------------

class TestFleetResult:
    def test_counts(self):
        fr = FleetResult(
            suite_name="Test",
            results=[
                TargetResult("a", "local", report=_mock_report()),
                TargetResult("b", "remote", error="fail"),
                TargetResult("c", "local", report=_mock_report(total=1, passed=0, failed=1)),
            ],
        )
        assert fr.total_targets == 3
        assert fr.targets_passed == 1
        assert fr.targets_failed == 2

    def test_to_dict(self):
        fr = FleetResult(
            suite_name="Test",
            results=[TargetResult("a", "local", report=_mock_report())],
            total_duration=5.0,
        )
        d = fr.to_dict()
        assert d["suite_name"] == "Test"
        assert d["total_targets"] == 1
        assert d["targets_passed"] == 1
        assert d["targets_failed"] == 0
        assert d["total_duration"] == 5.0
        assert len(d["results"]) == 1

    def test_empty_fleet(self):
        fr = FleetResult(suite_name="Test")
        assert fr.total_targets == 0
        assert fr.targets_passed == 0
        assert fr.targets_failed == 0


# ---------------------------------------------------------------------------
# _make_safe_dirname
# ---------------------------------------------------------------------------

class TestMakeSafeDirname:
    def test_url(self):
        assert _make_safe_dirname("http://localhost:8188") == "http-localhost-8188"

    def test_spaces_and_slashes(self):
        assert _make_safe_dirname("NVIDIA L40S") == "NVIDIA-L40S"

    def test_empty_string(self):
        assert _make_safe_dirname("") == "target"

    def test_only_special_chars(self):
        assert _make_safe_dirname("://") == "target"

    def test_normal_name(self):
        assert _make_safe_dirname("my-pod-01") == "my-pod-01"


# ---------------------------------------------------------------------------
# parse_target_spec
# ---------------------------------------------------------------------------

class TestParseTargetSpec:
    def test_local_with_url(self):
        t = parse_target_spec("local:http://localhost:8188")
        assert isinstance(t, LocalTarget)
        assert t.name == "http://localhost:8188"
        assert t.kind == "local"

    def test_local_without_scheme(self):
        t = parse_target_spec("local:localhost:8188")
        assert isinstance(t, LocalTarget)
        assert t._url == "http://localhost:8188"

    def test_local_with_label(self):
        t = parse_target_spec("local:http://localhost:8188,label=my-local")
        assert isinstance(t, LocalTarget)
        assert t.name == "my-local"

    def test_remote_with_url(self):
        t = parse_target_spec("remote:https://mybox.ts.net:9189")
        assert isinstance(t, RemoteTarget)
        assert t.kind == "remote"

    def test_remote_without_scheme(self):
        t = parse_target_spec("remote:mybox.ts.net:9189")
        assert isinstance(t, RemoteTarget)
        assert t._server_url == "https://mybox.ts.net:9189"

    def test_remote_with_install(self):
        t = parse_target_spec("remote:https://box:9189,install=dev")
        assert isinstance(t, RemoteTarget)
        assert t._install_name == "dev"

    def test_runpod_basic(self):
        t = parse_target_spec("runpod:NVIDIA L40S")
        assert isinstance(t, EphemeralTarget)
        assert t._gpu_type == "NVIDIA L40S"
        assert t.kind == "runpod"

    def test_runpod_with_label(self):
        t = parse_target_spec("runpod:NVIDIA A100,label=a100-test")
        assert isinstance(t, EphemeralTarget)
        assert t.name == "a100-test"

    def test_runpod_with_options(self):
        t = parse_target_spec("runpod:NVIDIA L40S,image=my-image,volume_id=vol123")
        assert isinstance(t, EphemeralTarget)
        assert t._image == "my-image"
        assert t._volume_id == "vol123"

    def test_invalid_no_colon(self):
        with pytest.raises(ValueError, match="Invalid target spec"):
            parse_target_spec("localhost8188")

    def test_invalid_kind(self):
        with pytest.raises(ValueError, match="Unknown target kind"):
            parse_target_spec("docker:myimage")

    def test_empty_value(self):
        with pytest.raises(ValueError, match="requires a value"):
            parse_target_spec("local:")

    def test_case_insensitive_kind(self):
        t = parse_target_spec("LOCAL:http://localhost:8188")
        assert isinstance(t, LocalTarget)

    def test_remote_default_install(self):
        t = parse_target_spec("remote:https://box:9189")
        assert isinstance(t, RemoteTarget)
        assert t._install_name == "main"


# ---------------------------------------------------------------------------
# RemoteTarget._resolve_comfy_url
# ---------------------------------------------------------------------------

class TestRemoteTargetResolveUrl:
    def test_port_replacement(self):
        t = RemoteTarget("https://mybox.ts.net:9189")
        assert t._resolve_comfy_url() == "https://mybox.ts.net:8188"

    def test_runpod_proxy_pattern(self):
        t = RemoteTarget("https://pod123-9189.proxy.runpod.net")
        assert t._resolve_comfy_url() == "https://pod123-8188.proxy.runpod.net"

    def test_fallback_port(self):
        t = RemoteTarget("https://mybox.ts.net:7777")
        assert t._resolve_comfy_url() == "https://mybox.ts.net:8188"


# ---------------------------------------------------------------------------
# LocalTarget.run (mocked)
# ---------------------------------------------------------------------------

class TestLocalTargetRun:
    @patch("comfy_runner.testing.fleet.write_report", return_value={})
    @patch("comfy_runner.testing.fleet.build_report")
    @patch("comfy_runner.testing.fleet.run_suite")
    def test_success(self, mock_run_suite, mock_build, mock_write, tmp_path):
        mock_build.return_value = _mock_report()
        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        target = LocalTarget("http://localhost:8188")
        result = target.run(suite, tmp_path / "output")

        assert result.passed is True
        assert result.target_kind == "local"
        assert result.error is None
        mock_run_suite.assert_called_once()

    @patch("comfy_runner.testing.fleet.run_suite")
    def test_error_handled(self, mock_run_suite, tmp_path):
        mock_run_suite.side_effect = RuntimeError("Connection refused")
        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        target = LocalTarget("http://localhost:8188")
        result = target.run(suite, tmp_path / "output")

        assert result.passed is False
        assert "Connection refused" in result.error


# ---------------------------------------------------------------------------
# RemoteTarget.run (mocked)
# ---------------------------------------------------------------------------

class TestRemoteTargetRun:
    @patch("comfy_runner.testing.fleet.write_report", return_value={})
    @patch("comfy_runner.testing.fleet.build_report")
    @patch("comfy_runner.testing.fleet.run_suite")
    @patch("comfy_runner.hosted.remote.RemoteRunner.get_status")
    def test_success(self, mock_status, mock_run_suite, mock_build, mock_write, tmp_path):
        mock_status.return_value = {"running": True}
        mock_build.return_value = _mock_report()
        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        target = RemoteTarget("https://mybox:9189")
        result = target.run(suite, tmp_path / "output")

        assert result.passed is True
        assert result.target_kind == "remote"

    @patch("comfy_runner.hosted.remote.RemoteRunner.get_status")
    def test_error_handled(self, mock_status, tmp_path):
        mock_status.side_effect = RuntimeError("Server down")
        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        target = RemoteTarget("https://mybox:9189")
        # run_suite will fail because ComfyTestClient can't connect
        # but error is caught
        result = target.run(suite, tmp_path / "output")
        # Status error is caught and ignored, so run_suite tries and fails
        assert result.error is not None or result.report is not None


# ---------------------------------------------------------------------------
# EphemeralTarget.run (mocked)
# ---------------------------------------------------------------------------

class TestEphemeralTargetRun:
    @patch("comfy_runner.testing.runpod.run_on_runpod")
    def test_success(self, mock_run_on_runpod, tmp_path):
        rp_result = MagicMock()
        rp_result.error = None
        rp_result.report = _mock_report()
        mock_run_on_runpod.return_value = rp_result

        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        target = EphemeralTarget("NVIDIA L40S")
        result = target.run(suite, tmp_path / "output")

        assert result.passed is True
        assert result.target_kind == "runpod"
        mock_run_on_runpod.assert_called_once()

    @patch("comfy_runner.testing.runpod.run_on_runpod")
    def test_error_propagated(self, mock_run_on_runpod, tmp_path):
        rp_result = MagicMock()
        rp_result.error = "Pod creation failed"
        rp_result.report = None
        mock_run_on_runpod.return_value = rp_result

        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        target = EphemeralTarget("NVIDIA A100")
        result = target.run(suite, tmp_path / "output")

        assert result.passed is False
        assert "Pod creation failed" in result.error

    @patch("comfy_runner.testing.runpod.run_on_runpod")
    def test_output_dir_passed_to_config(self, mock_run_on_runpod, tmp_path):
        rp_result = MagicMock()
        rp_result.error = None
        rp_result.report = _mock_report()
        mock_run_on_runpod.return_value = rp_result

        suite_dir = _make_suite(tmp_path)
        from comfy_runner.testing.suite import load_suite
        suite = load_suite(suite_dir)

        out_dir = tmp_path / "fleet" / "runpod-l40s"
        target = EphemeralTarget("NVIDIA L40S")
        target.run(suite, out_dir)

        # Verify output_dir was passed to RunPodTestConfig
        config = mock_run_on_runpod.call_args[0][0]
        assert config.output_dir == str(out_dir)


# ---------------------------------------------------------------------------
# run_fleet (mocked targets)
# ---------------------------------------------------------------------------

class TestRunFleet:
    def test_parallel_execution(self, tmp_path):
        suite_dir = _make_suite(tmp_path)

        target_a = MagicMock()
        target_a.name = "target-a"
        target_a.kind = "local"
        target_a.run.return_value = TargetResult(
            target_name="target-a", target_kind="local",
            report=_mock_report(), duration=1.0,
        )

        target_b = MagicMock()
        target_b.name = "target-b"
        target_b.kind = "local"
        target_b.run.return_value = TargetResult(
            target_name="target-b", target_kind="local",
            report=_mock_report(), duration=2.0,
        )

        out_dir = tmp_path / "fleet_out"
        result = run_fleet(
            targets=[target_a, target_b],
            suite_path=str(suite_dir),
            output_dir=out_dir,
        )

        assert result.total_targets == 2
        assert result.targets_passed == 2
        assert result.targets_failed == 0
        target_a.run.assert_called_once()
        target_b.run.assert_called_once()

    def test_preserves_input_order(self, tmp_path):
        """Results are returned in target input order, not completion order."""
        suite_dir = _make_suite(tmp_path)

        targets = []
        for name in ("first", "second", "third"):
            t = MagicMock()
            t.name = name
            t.kind = "local"
            t.run.return_value = TargetResult(
                target_name=name, target_kind="local",
                report=_mock_report(), duration=1.0,
            )
            targets.append(t)

        out_dir = tmp_path / "fleet_out"
        result = run_fleet(targets=targets, suite_path=str(suite_dir), output_dir=out_dir)

        names = [r.target_name for r in result.results]
        assert names == ["first", "second", "third"]

    def test_one_failure_does_not_block_others(self, tmp_path):
        suite_dir = _make_suite(tmp_path)

        good = MagicMock()
        good.name = "good"
        good.kind = "local"
        good.run.return_value = TargetResult(
            target_name="good", target_kind="local",
            report=_mock_report(), duration=1.0,
        )

        bad = MagicMock()
        bad.name = "bad"
        bad.kind = "local"
        bad.run.return_value = TargetResult(
            target_name="bad", target_kind="local",
            error="Connection refused",
        )

        out_dir = tmp_path / "fleet_out"
        result = run_fleet(
            targets=[good, bad],
            suite_path=str(suite_dir),
            output_dir=out_dir,
        )

        assert result.total_targets == 2
        assert result.targets_passed == 1
        assert result.targets_failed == 1
        assert result.results[0].passed is True
        assert result.results[1].error == "Connection refused"

    def test_empty_fleet(self, tmp_path):
        suite_dir = _make_suite(tmp_path)
        out_dir = tmp_path / "fleet_out"
        result = run_fleet(
            targets=[],
            suite_path=str(suite_dir),
            output_dir=out_dir,
        )
        assert result.total_targets == 0

    def test_per_target_output_dirs(self, tmp_path):
        """Each target gets its own output subdirectory."""
        suite_dir = _make_suite(tmp_path)

        target_a = MagicMock()
        target_a.name = "alpha"
        target_a.kind = "local"
        target_a.run.return_value = TargetResult(
            target_name="alpha", target_kind="local",
            report=_mock_report(),
        )

        target_b = MagicMock()
        target_b.name = "beta"
        target_b.kind = "remote"
        target_b.run.return_value = TargetResult(
            target_name="beta", target_kind="remote",
            report=_mock_report(),
        )

        out_dir = tmp_path / "fleet_out"
        run_fleet(
            targets=[target_a, target_b],
            suite_path=str(suite_dir),
            output_dir=out_dir,
        )

        # Check that run() was called with per-target dirs (indexed to avoid collisions)
        a_dir = target_a.run.call_args[1].get("output_dir") or target_a.run.call_args[0][1]
        b_dir = target_b.run.call_args[1].get("output_dir") or target_b.run.call_args[0][1]
        assert a_dir != b_dir
        assert "alpha" in str(a_dir)
        assert "beta" in str(b_dir)
        # Dirs should be prefixed with index
        assert "0-" in str(a_dir)
        assert "1-" in str(b_dir)

    def test_writes_fleet_report_json(self, tmp_path):
        suite_dir = _make_suite(tmp_path)

        target = MagicMock()
        target.name = "t1"
        target.kind = "local"
        target.run.return_value = TargetResult(
            target_name="t1", target_kind="local",
            report=_mock_report(),
        )

        out_dir = tmp_path / "fleet_out"
        run_fleet(
            targets=[target],
            suite_path=str(suite_dir),
            output_dir=out_dir,
        )

        report_path = out_dir / "fleet-report.json"
        assert report_path.is_file()
        data = json.loads(report_path.read_text())
        assert data["suite_name"] == "Test Suite"
        assert data["total_targets"] == 1

    def test_max_workers_respected(self, tmp_path):
        """max_workers is passed to ThreadPoolExecutor."""
        suite_dir = _make_suite(tmp_path)

        target = MagicMock()
        target.name = "t1"
        target.kind = "local"
        target.run.return_value = TargetResult(
            target_name="t1", target_kind="local",
            report=_mock_report(),
        )

        out_dir = tmp_path / "fleet_out"
        # Just verify it doesn't crash with max_workers=1
        result = run_fleet(
            targets=[target],
            suite_path=str(suite_dir),
            output_dir=out_dir,
            max_workers=1,
        )
        assert result.total_targets == 1

    def test_target_exception_captured(self, tmp_path):
        """If a target's run() raises (not returns error), it's still captured."""
        suite_dir = _make_suite(tmp_path)

        target = MagicMock()
        target.name = "exploder"
        target.kind = "local"
        target.run.side_effect = RuntimeError("kaboom")

        out_dir = tmp_path / "fleet_out"
        result = run_fleet(
            targets=[target],
            suite_path=str(suite_dir),
            output_dir=out_dir,
        )

        assert result.total_targets == 1
        assert result.results[0].error == "kaboom"
        assert result.targets_failed == 1


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

class TestFleetRenderers:
    def test_render_fleet_console_all_pass(self):
        fr = FleetResult(
            suite_name="Smoke",
            results=[
                TargetResult("local", "local", report=_mock_report(), duration=1.0),
                TargetResult("remote", "remote", report=_mock_report(), duration=2.0),
            ],
            total_duration=2.5,
        )
        output = render_fleet_console(fr)
        assert "all 2 target(s) passed" in output
        assert "Smoke" in output
        assert "local" in output
        assert "remote" in output

    def test_render_fleet_console_with_failure(self):
        fr = FleetResult(
            suite_name="Smoke",
            results=[
                TargetResult("ok", "local", report=_mock_report(), duration=1.0),
                TargetResult("bad", "remote", error="timeout"),
            ],
            total_duration=5.0,
        )
        output = render_fleet_console(fr)
        assert "1/2 target(s) failed" in output
        assert "timeout" in output

    def test_render_fleet_json(self):
        fr = FleetResult(
            suite_name="Smoke",
            results=[TargetResult("t1", "local", report=_mock_report())],
            total_duration=1.0,
        )
        output = render_fleet_json(fr)
        data = json.loads(output)
        assert data["suite_name"] == "Smoke"
        assert data["total_targets"] == 1

    def test_render_fleet_console_no_report(self):
        fr = FleetResult(
            suite_name="Test",
            results=[
                TargetResult("mystery", "local"),
            ],
        )
        output = render_fleet_console(fr)
        assert "no result" in output


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCLIFleet:
    def test_fleet_subcommand_exists(self):
        from comfy_runner_cli.cli import main
        # Fleet requires --target, should fail without it
        with pytest.raises(SystemExit):
            main(["test", "fleet", "/fake/suite"])

    def test_fleet_invalid_suite(self, capsys):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "fleet", "/nonexistent",
                  "--target", "local:http://localhost:8188"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False

    def test_fleet_invalid_target_spec(self, capsys):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "fleet", "/fake/suite",
                  "--target", "badspec"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "Invalid target spec" in out["error"]

    def test_fleet_unknown_target_kind(self, capsys):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "fleet", "/fake/suite",
                  "--target", "docker:myimage"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "Unknown target kind" in out["error"]

    def test_pr_branch_commit_exclusive(self):
        from comfy_runner_cli.cli import main
        with pytest.raises(SystemExit):
            main(["--json", "test", "fleet", "/fake/suite",
                  "--target", "local:http://localhost:8188",
                  "--pr", "1", "--branch", "main"])

    @patch("comfy_runner.testing.fleet.run_fleet")
    def test_fleet_json_output(self, mock_run_fleet, tmp_path, capsys):
        from comfy_runner_cli.cli import main

        suite_dir = _make_suite(tmp_path)

        mock_run_fleet.return_value = FleetResult(
            suite_name="Test Suite",
            results=[
                TargetResult("local", "local", report=_mock_report(), duration=1.0),
            ],
            total_duration=1.5,
        )

        main(["--json", "test", "fleet", str(suite_dir),
              "--target", "local:http://localhost:8188"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["total_targets"] == 1
        assert out["targets_passed"] == 1

    @patch("comfy_runner.testing.fleet.run_fleet")
    def test_fleet_failure_exit_code(self, mock_run_fleet, tmp_path, capsys):
        from comfy_runner_cli.cli import main

        suite_dir = _make_suite(tmp_path)

        mock_run_fleet.return_value = FleetResult(
            suite_name="Test Suite",
            results=[
                TargetResult("bad", "local", error="fail"),
            ],
            total_duration=1.0,
        )

        with pytest.raises(SystemExit) as exc_info:
            main(["--json", "test", "fleet", str(suite_dir),
                  "--target", "local:http://localhost:8188"])
        assert exc_info.value.code == 1

    @patch("comfy_runner.testing.fleet.run_fleet")
    def test_fleet_multiple_targets(self, mock_run_fleet, tmp_path, capsys):
        from comfy_runner_cli.cli import main

        suite_dir = _make_suite(tmp_path)

        mock_run_fleet.return_value = FleetResult(
            suite_name="Test Suite",
            results=[
                TargetResult("a", "local", report=_mock_report()),
                TargetResult("b", "remote", report=_mock_report()),
            ],
            total_duration=2.0,
        )

        main(["--json", "test", "fleet", str(suite_dir),
              "--target", "local:http://localhost:8188",
              "--target", "remote:https://mybox:9189"])
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["total_targets"] == 2

    @patch("comfy_runner.testing.fleet.run_fleet")
    def test_fleet_rich_output(self, mock_run_fleet, tmp_path, capsys):
        from comfy_runner_cli.cli import main

        suite_dir = _make_suite(tmp_path)

        mock_run_fleet.return_value = FleetResult(
            suite_name="Test Suite",
            results=[
                TargetResult("local", "local", report=_mock_report(), duration=1.0),
            ],
            total_duration=1.5,
        )

        main(["test", "fleet", str(suite_dir),
              "--target", "local:http://localhost:8188"])
        out = capsys.readouterr().out
        assert "Fleet" in out or "target" in out

    @patch("comfy_runner.testing.fleet.run_fleet")
    def test_fleet_max_workers_arg(self, mock_run_fleet, tmp_path, capsys):
        from comfy_runner_cli.cli import main

        suite_dir = _make_suite(tmp_path)

        mock_run_fleet.return_value = FleetResult(
            suite_name="Test Suite",
            results=[
                TargetResult("t1", "local", report=_mock_report()),
            ],
            total_duration=1.0,
        )

        main(["--json", "test", "fleet", str(suite_dir),
              "--target", "local:http://localhost:8188",
              "--max-workers", "2"])
        mock_run_fleet.assert_called_once()
        call_kwargs = mock_run_fleet.call_args
        # max_workers should be passed through
        assert call_kwargs[1].get("max_workers") == 2 or call_kwargs.kwargs.get("max_workers") == 2


# ---------------------------------------------------------------------------
# RunPodTestConfig / RunPodTestResult extensions
# ---------------------------------------------------------------------------

class TestRunPodExtensions:
    def test_config_output_dir_field(self):
        from comfy_runner.testing.runpod import RunPodTestConfig
        cfg = RunPodTestConfig(suite_path="/test", output_dir="/custom/dir")
        assert cfg.output_dir == "/custom/dir"

    def test_config_output_dir_default_none(self):
        from comfy_runner.testing.runpod import RunPodTestConfig
        cfg = RunPodTestConfig(suite_path="/test")
        assert cfg.output_dir is None

    def test_result_report_field(self):
        from comfy_runner.testing.runpod import RunPodTestResult
        r = RunPodTestResult(pod_id="p1", pod_name="test", server_url="http://x")
        assert r.report is None
        r.report = _mock_report()
        assert r.report is not None
