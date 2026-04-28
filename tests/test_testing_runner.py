"""Tests for comfy_runner.testing.runner — test orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from comfy_runner.testing.client import ComfyTestClient, OutputFile, PromptResult
from comfy_runner.testing.runner import (
    SuiteRun,
    WorkflowResult,
    _apply_overrides,
    run_suite,
    run_workflow,
)
from comfy_runner.testing.suite import load_suite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_suite_dir(tmp_path: Path) -> Path:
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "suite.json").write_text(json.dumps({"name": "Test"}))
    wf_dir = suite_dir / "workflows"
    wf_dir.mkdir()
    (wf_dir / "wf1.json").write_text(json.dumps({
        "1": {"class_type": "KSampler", "inputs": {"seed": 0}},
    }))
    (wf_dir / "wf2.json").write_text(json.dumps({
        "1": {"class_type": "EmptyLatentImage", "inputs": {}},
    }))
    return suite_dir


def _mock_prompt_result(prompt_id: str = "p1") -> PromptResult:
    return PromptResult(
        prompt_id=prompt_id,
        status="success",
        outputs={"9": [OutputFile(node_id="9", filename="out.png", subfolder="", type="output")]},
        execution_time=1.5,
    )


# ---------------------------------------------------------------------------
# _apply_overrides
# ---------------------------------------------------------------------------

class TestApplyOverrides:
    def test_seed_override(self):
        wf = {
            "1": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20}},
            "2": {"class_type": "Other", "inputs": {"noise_seed": 0}},
        }
        result = _apply_overrides(wf, {"seed": 42})
        assert result["1"]["inputs"]["seed"] == 42
        assert result["2"]["inputs"]["noise_seed"] == 42
        assert result["1"]["inputs"]["steps"] == 20

    def test_no_overrides(self):
        wf = {"1": {"inputs": {"seed": 5}}}
        result = _apply_overrides(wf, {})
        assert result["1"]["inputs"]["seed"] == 5

    def test_seed_none_skips(self):
        wf = {"1": {"inputs": {"seed": 5}}}
        result = _apply_overrides(wf, {"seed": None})
        assert result["1"]["inputs"]["seed"] == 5


# ---------------------------------------------------------------------------
# WorkflowResult
# ---------------------------------------------------------------------------

class TestWorkflowResult:
    def test_passed_true(self):
        r = WorkflowResult(
            workflow_name="test",
            workflow_path=Path("test.json"),
            prompt_result=_mock_prompt_result(),
        )
        assert r.passed is True

    def test_passed_false_no_result(self):
        r = WorkflowResult(
            workflow_name="test",
            workflow_path=Path("test.json"),
            error="failed",
        )
        assert r.passed is False

    def test_passed_false_with_error(self):
        r = WorkflowResult(
            workflow_name="test",
            workflow_path=Path("test.json"),
            prompt_result=_mock_prompt_result(),
            error="something wrong",
        )
        assert r.passed is False


# ---------------------------------------------------------------------------
# TestRun
# ---------------------------------------------------------------------------

class TestSuiteRun:
    def test_summary(self):
        run = SuiteRun(
            suite_name="Test",
            suite_path=Path("/suite"),
            output_dir=Path("/out"),
            started_at=0.0,
            finished_at=2.5,
            results=[
                WorkflowResult("w1", Path("w1.json"), prompt_result=_mock_prompt_result()),
                WorkflowResult("w2", Path("w2.json"), error="boom"),
            ],
        )
        assert run.total == 2
        assert run.passed == 1
        assert run.failed == 1
        assert run.duration == 2.5

        s = run.summary()
        assert s["suite"] == "Test"
        assert s["total"] == 2
        assert s["passed"] == 1
        assert s["failed"] == 1


# ---------------------------------------------------------------------------
# run_workflow
# ---------------------------------------------------------------------------

class TestRunWorkflow:
    def test_success(self, tmp_path):
        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.return_value = _mock_prompt_result()

        wf_path = tmp_path / "wf.json"
        wf_path.write_text(json.dumps({"1": {"inputs": {}}}))

        out_dir = tmp_path / "output"
        result = run_workflow(client, wf_path, out_dir)

        assert result.passed is True
        assert result.workflow_name == "wf"
        client.run_workflow.assert_called_once()

    def test_invalid_json(self, tmp_path):
        client = MagicMock(spec=ComfyTestClient)
        wf_path = tmp_path / "bad.json"
        wf_path.write_text("not json")

        result = run_workflow(client, wf_path, tmp_path / "output")
        assert result.passed is False
        assert "Failed to load workflow" in result.error

    def test_runtime_error(self, tmp_path):
        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.side_effect = RuntimeError("timeout")

        wf_path = tmp_path / "wf.json"
        wf_path.write_text(json.dumps({"1": {}}))

        result = run_workflow(client, wf_path, tmp_path / "output")
        assert result.passed is False
        assert "timeout" in result.error

    def test_applies_overrides(self, tmp_path):
        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.return_value = _mock_prompt_result()

        wf_path = tmp_path / "wf.json"
        wf_path.write_text(json.dumps({"1": {"inputs": {"seed": 0}}}))

        run_workflow(client, wf_path, tmp_path / "output", overrides={"seed": 99})

        # Check the workflow passed to client had seed overridden
        called_wf = client.run_workflow.call_args[0][0]
        assert called_wf["1"]["inputs"]["seed"] == 99


# ---------------------------------------------------------------------------
# run_suite
# ---------------------------------------------------------------------------

class TestRunSuite:
    def test_runs_all_workflows(self, tmp_path):
        suite_dir = _make_suite_dir(tmp_path)
        suite = load_suite(suite_dir)

        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.return_value = _mock_prompt_result()

        out_dir = tmp_path / "run_output"
        test_run = run_suite(client, suite, out_dir)

        assert test_run.total == 2
        assert test_run.passed == 2
        assert test_run.failed == 0
        assert (out_dir / "summary.json").is_file()

        summary = json.loads((out_dir / "summary.json").read_text())
        assert summary["total"] == 2

    def test_handles_mixed_results(self, tmp_path):
        suite_dir = _make_suite_dir(tmp_path)
        suite = load_suite(suite_dir)

        client = MagicMock(spec=ComfyTestClient)
        # First workflow succeeds, second fails
        client.run_workflow.side_effect = [
            _mock_prompt_result(),
            RuntimeError("GPU OOM"),
        ]

        out_dir = tmp_path / "run_output"
        test_run = run_suite(client, suite, out_dir)

        assert test_run.total == 2
        assert test_run.passed == 1
        assert test_run.failed == 1

    def test_applies_suite_overrides(self, tmp_path):
        suite_dir = _make_suite_dir(tmp_path)
        (suite_dir / "config.json").write_text(json.dumps({"overrides": {"seed": 42}}))
        suite = load_suite(suite_dir)

        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.return_value = _mock_prompt_result()

        out_dir = tmp_path / "run_output"
        run_suite(client, suite, out_dir)

        # Verify seed was overridden in the first workflow call
        first_call_wf = client.run_workflow.call_args_list[0][0][0]
        assert first_call_wf["1"]["inputs"]["seed"] == 42

    def test_marks_baseline_status(self, tmp_path):
        suite_dir = _make_suite_dir(tmp_path)
        bl_dir = suite_dir / "baselines" / "wf1"
        bl_dir.mkdir(parents=True)
        (bl_dir / "output_0.png").write_bytes(b"baseline")
        suite = load_suite(suite_dir)

        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.return_value = _mock_prompt_result()

        out_dir = tmp_path / "run_output"
        test_run = run_suite(client, suite, out_dir)

        wf1_result = next(r for r in test_run.results if r.workflow_name == "wf1")
        wf2_result = next(r for r in test_run.results if r.workflow_name == "wf2")
        assert wf1_result.has_baseline is True
        assert wf2_result.has_baseline is False

    def test_compares_against_baselines(self, tmp_path):
        suite_dir = _make_suite_dir(tmp_path)
        # Create a baseline with matching filename
        bl_dir = suite_dir / "baselines" / "wf1"
        bl_dir.mkdir(parents=True)
        (bl_dir / "out.png").write_bytes(b"baseline data")
        suite = load_suite(suite_dir)

        # Mock client that returns a result with a local_path set
        out_dir = tmp_path / "run_output"
        wf1_out = out_dir / "wf1" / "9"
        wf1_out.mkdir(parents=True)
        test_file = wf1_out / "out.png"
        test_file.write_bytes(b"test data")

        def _mock_run(wf, od, timeout=600, cancelled=None):
            result = _mock_prompt_result()
            # Set local_path on the output file
            result.outputs["9"][0].local_path = test_file
            return result

        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.side_effect = _mock_run

        test_run = run_suite(client, suite, out_dir)

        # wf1 has a baseline with matching filename → comparisons should be populated
        assert "wf1" in test_run.comparisons
        assert len(test_run.comparisons["wf1"]) == 1
        assert test_run.comparisons["wf1"][0].result.method == "existence"
        assert test_run.comparisons["wf1"][0].result.passed is True
        # wf2 has no baseline → no comparisons
        assert "wf2" not in test_run.comparisons


# ---------------------------------------------------------------------------
# Watchdog cancellation
# ---------------------------------------------------------------------------

class TestRunSuiteCancellation:
    def test_pre_set_event_aborts_immediately(self, tmp_path):
        """When ``cancelled`` is already set, no workflows run and a
        synthetic ``__watchdog__`` failure row is appended."""
        import threading

        suite_dir = _make_suite_dir(tmp_path)
        suite = load_suite(suite_dir)
        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.side_effect = AssertionError(
            "should not be called when cancelled is pre-set"
        )

        out_dir = tmp_path / "run_output"
        cancelled = threading.Event()
        cancelled.set()

        test_run = run_suite(client, suite, out_dir, cancelled=cancelled)

        assert test_run.timed_out is True
        assert test_run.aborted_reason == "overrun"
        assert test_run.failed >= 1
        # Synthetic row inserted because no workflow ran.
        assert any(r.workflow_name == "__watchdog__" for r in test_run.results)
        client.run_workflow.assert_not_called()

    def test_event_set_between_workflows(self, tmp_path):
        """Setting cancelled after the first workflow runs prevents the
        second workflow from running."""
        import threading

        suite_dir = _make_suite_dir(tmp_path)
        suite = load_suite(suite_dir)

        cancelled = threading.Event()
        call_count = {"n": 0}

        def _run(wf, od, timeout=600, cancelled=None):
            call_count["n"] += 1
            # Trip the watchdog after the first workflow completes.
            if cancelled is not None:
                cancelled.set()
            return _mock_prompt_result()

        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.side_effect = _run

        out_dir = tmp_path / "run_output"
        test_run = run_suite(client, suite, out_dir, cancelled=cancelled)

        assert call_count["n"] == 1, "second workflow must be skipped"
        assert test_run.timed_out is True
        # First workflow result is preserved; synthetic row added because
        # no in-flight workflow recorded ``error="overrun"``.
        assert any(r.error == "overrun" for r in test_run.results)


class TestRunWorkflowCancellation:
    def test_aborts_on_watchdog_event(self, tmp_path):
        """``WatchdogAborted`` raised inside ``client.run_workflow``
        becomes a result row with ``error='overrun'``."""
        import threading
        from comfy_runner.testing.client import WatchdogAborted

        suite_dir = _make_suite_dir(tmp_path)
        wf_path = suite_dir / "workflows" / "wf1.json"
        client = MagicMock(spec=ComfyTestClient)
        client.run_workflow.side_effect = WatchdogAborted("aborted")

        result = run_workflow(
            client, wf_path, tmp_path / "out",
            cancelled=threading.Event(),
        )
        assert result.passed is False
        assert result.error == "overrun"
