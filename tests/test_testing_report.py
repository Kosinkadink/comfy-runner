"""Tests for comfy_runner.testing.report — shared data model and renderers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_runner.testing.client import OutputFile, PromptResult
from comfy_runner.testing.compare.registry import CompareResult
from comfy_runner.testing.report import (
    ComparisonEntry,
    SuiteReport,
    WorkflowReport,
    build_report,
    render_console,
    render_html,
    render_json,
    render_markdown,
    write_report,
)
from comfy_runner.testing.runner import SuiteRun, WorkflowResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_suite_run(
    name: str = "Test Suite",
    passed_count: int = 2,
    failed_count: int = 0,
) -> SuiteRun:
    """Create a SuiteRun with some results."""
    results: list[WorkflowResult] = []
    for i in range(passed_count):
        results.append(WorkflowResult(
            workflow_name=f"wf_pass_{i}",
            workflow_path=Path(f"wf_pass_{i}.json"),
            prompt_result=PromptResult(
                prompt_id=f"p{i}",
                status="success",
                outputs={"9": [OutputFile("9", f"out_{i}.png", "", "output")]},
                execution_time=1.5 + i,
            ),
            has_baseline=True,
        ))
    for i in range(failed_count):
        results.append(WorkflowResult(
            workflow_name=f"wf_fail_{i}",
            workflow_path=Path(f"wf_fail_{i}.json"),
            error=f"GPU OOM on node {i}",
        ))
    return SuiteRun(
        suite_name=name,
        suite_path=Path("/suite"),
        output_dir=Path("/output"),
        results=results,
        started_at=0.0,
        finished_at=3.5,
    )


def _make_comparisons() -> dict[str, list[ComparisonEntry]]:
    """Create comparison entries for wf_pass_0."""
    return {
        "wf_pass_0": [
            ComparisonEntry(
                baseline_file="baseline/out_0.png",
                test_file="out_0.png",
                result=CompareResult(method="ssim", score=0.9823, passed=True, threshold=0.95),
            ),
        ],
    }


def _make_failed_comparisons() -> dict[str, list[ComparisonEntry]]:
    """Create comparison entries with a failure."""
    return {
        "wf_pass_0": [
            ComparisonEntry(
                baseline_file="baseline/out_0.png",
                test_file="out_0.png",
                result=CompareResult(method="ssim", score=0.8100, passed=False, threshold=0.95),
            ),
        ],
    }


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_basic(self):
        run = _make_suite_run(passed_count=2, failed_count=1)
        report = build_report(run)
        assert report.suite_name == "Test Suite"
        assert report.total == 3
        assert report.passed == 2
        assert report.failed == 1
        assert len(report.workflows) == 3

    def test_with_comparisons(self):
        run = _make_suite_run()
        comparisons = _make_comparisons()
        report = build_report(run, comparisons=comparisons)
        wf0 = report.workflows[0]
        assert len(wf0.comparisons) == 1
        assert wf0.comparisons[0].result.score == 0.9823

    def test_with_target_info(self):
        run = _make_suite_run()
        report = build_report(run, target_info={"name": "home-4090", "gpu": "RTX 4090"})
        assert report.target_info["name"] == "home-4090"

    def test_timestamp_present(self):
        run = _make_suite_run()
        report = build_report(run)
        assert report.timestamp  # non-empty ISO timestamp

    def test_no_comparisons_default(self):
        run = _make_suite_run()
        report = build_report(run)
        for wf in report.workflows:
            assert wf.comparisons == []


# ---------------------------------------------------------------------------
# WorkflowReport.comparison_passed
# ---------------------------------------------------------------------------

class TestWorkflowReportProperties:
    def test_comparison_passed_all_pass(self):
        wf = WorkflowReport(
            name="test", passed=True,
            comparisons=[
                ComparisonEntry("b", "t", CompareResult("ssim", 0.99, True, 0.95)),
                ComparisonEntry("b2", "t2", CompareResult("phash", 0.98, True, 0.90)),
            ],
        )
        assert wf.comparison_passed is True

    def test_comparison_passed_one_fails(self):
        wf = WorkflowReport(
            name="test", passed=True,
            comparisons=[
                ComparisonEntry("b", "t", CompareResult("ssim", 0.99, True, 0.95)),
                ComparisonEntry("b2", "t2", CompareResult("ssim", 0.80, False, 0.95)),
            ],
        )
        assert wf.comparison_passed is False

    def test_comparison_passed_no_comparisons(self):
        wf = WorkflowReport(name="test", passed=True)
        assert wf.comparison_passed is True


# ---------------------------------------------------------------------------
# SuiteReport.to_dict
# ---------------------------------------------------------------------------

class TestSuiteReportToDict:
    def test_serializable(self):
        run = _make_suite_run()
        comparisons = _make_comparisons()
        report = build_report(run, comparisons=comparisons)
        d = report.to_dict()
        # Must be JSON-serializable
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["suite_name"] == "Test Suite"

    def test_diff_artifact_converted_to_str(self):
        run = _make_suite_run()
        comparisons = {
            "wf_pass_0": [ComparisonEntry(
                "b", "t",
                CompareResult("ssim", 0.9, True, 0.95, diff_artifact=Path("/tmp/diff.png")),
            )],
        }
        report = build_report(run, comparisons=comparisons)
        d = report.to_dict()
        artifact = d["workflows"][0]["comparisons"][0]["result"]["diff_artifact"]
        assert isinstance(artifact, str)


# ---------------------------------------------------------------------------
# render_json
# ---------------------------------------------------------------------------

class TestRenderJSON:
    def test_valid_json(self):
        report = build_report(_make_suite_run())
        text = render_json(report)
        data = json.loads(text)
        assert data["suite_name"] == "Test Suite"
        assert data["total"] == 2

    def test_with_comparisons(self):
        report = build_report(_make_suite_run(), comparisons=_make_comparisons())
        data = json.loads(render_json(report))
        comps = data["workflows"][0]["comparisons"]
        assert len(comps) == 1
        assert comps[0]["result"]["method"] == "ssim"


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

class TestRenderMarkdown:
    def test_all_passed(self):
        report = build_report(_make_suite_run())
        md = render_markdown(report)
        assert "✅" in md
        assert "2/2 passed" in md
        assert "| wf_pass_0 |" in md

    def test_with_failures(self):
        report = build_report(_make_suite_run(passed_count=1, failed_count=1))
        md = render_markdown(report)
        assert "❌" in md
        assert "1 failed" in md
        assert "GPU OOM" in md

    def test_comparison_table(self):
        report = build_report(_make_suite_run(), comparisons=_make_comparisons())
        md = render_markdown(report)
        assert "### Comparison Results" in md
        assert "ssim" in md
        assert "0.9823" in md

    def test_target_info(self):
        report = build_report(_make_suite_run(), target_info={"name": "home-4090"})
        md = render_markdown(report)
        assert "home-4090" in md

    def test_timestamp(self):
        report = build_report(_make_suite_run())
        md = render_markdown(report)
        assert "*Generated" in md


# ---------------------------------------------------------------------------
# render_console
# ---------------------------------------------------------------------------

class TestRenderConsole:
    def test_all_passed(self):
        report = build_report(_make_suite_run())
        text = render_console(report)
        assert "all 2 passed" in text
        assert "wf_pass_0" in text

    def test_with_failures(self):
        report = build_report(_make_suite_run(passed_count=1, failed_count=1))
        text = render_console(report)
        assert "1/2 failed" in text
        assert "GPU OOM" in text

    def test_comparison_details(self):
        report = build_report(_make_suite_run(), comparisons=_make_comparisons())
        text = render_console(report)
        assert "ssim" in text
        assert "0.9823" in text

    def test_failed_comparison(self):
        report = build_report(_make_suite_run(), comparisons=_make_failed_comparisons())
        text = render_console(report)
        assert "threshold" in text

    def test_target_info(self):
        report = build_report(_make_suite_run(), target_info={"name": "office-3090"})
        text = render_console(report)
        assert "office-3090" in text


# ---------------------------------------------------------------------------
# render_html
# ---------------------------------------------------------------------------

class TestRenderHTML:
    def test_valid_html(self):
        report = build_report(_make_suite_run())
        html = render_html(report)
        assert "<!DOCTYPE html>" in html
        assert "Test Suite" in html
        assert "</html>" in html

    def test_all_passed(self):
        report = build_report(_make_suite_run())
        html = render_html(report)
        assert "✅" in html
        assert "2 passed" in html

    def test_with_failures(self):
        report = build_report(_make_suite_run(passed_count=1, failed_count=1))
        html = render_html(report)
        assert "FAIL" in html
        assert "GPU OOM" in html

    def test_comparison_table(self):
        report = build_report(_make_suite_run(), comparisons=_make_comparisons())
        html = render_html(report)
        assert "<table>" in html
        assert "ssim" in html
        assert "0.9823" in html

    def test_html_escaping(self):
        run = _make_suite_run(name='Suite <script>alert("xss")</script>')
        report = build_report(run)
        html = render_html(report)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_target_info(self):
        report = build_report(_make_suite_run(), target_info={"name": "pod-a100"})
        html = render_html(report)
        assert "pod-a100" in html


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_writes_all_formats(self, tmp_path):
        report = build_report(_make_suite_run())
        written = write_report(report, tmp_path)
        assert "json" in written
        assert "html" in written
        assert "markdown" in written
        assert written["json"].exists()
        assert written["html"].exists()
        assert written["markdown"].exists()

    def test_writes_selected_formats(self, tmp_path):
        report = build_report(_make_suite_run())
        written = write_report(report, tmp_path, formats=["json"])
        assert "json" in written
        assert "html" not in written
        assert not (tmp_path / "report.html").exists()

    def test_console_format(self, tmp_path):
        report = build_report(_make_suite_run())
        written = write_report(report, tmp_path, formats=["console"])
        assert "console" in written
        assert written["console"].name == "report.txt"
        content = written["console"].read_text(encoding="utf-8")
        assert "all 2 passed" in content

    def test_creates_output_dir(self, tmp_path):
        report = build_report(_make_suite_run())
        out = tmp_path / "nested" / "dir"
        write_report(report, out, formats=["json"])
        assert (out / "report.json").exists()

    def test_json_roundtrip(self, tmp_path):
        report = build_report(_make_suite_run(), comparisons=_make_comparisons())
        write_report(report, tmp_path, formats=["json"])
        data = json.loads((tmp_path / "report.json").read_text())
        assert data["suite_name"] == "Test Suite"
        assert data["workflows"][0]["comparisons"][0]["result"]["score"] == 0.9823
