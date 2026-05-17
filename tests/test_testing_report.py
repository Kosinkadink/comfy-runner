"""Tests for comfy_runner.testing.report — shared data model and renderers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_runner.testing.client import OutputFile, PromptResult
from comfy_runner.testing.compare.registry import CompareResult
from comfy_runner.testing.report import (
    SuiteReport,
    WorkflowReport,
    build_report,
    render_console,
    render_html,
    render_json,
    render_markdown,
    write_report,
)
from comfy_runner.testing.runner import ComparisonEntry, SuiteRun, WorkflowResult


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

    def test_image_grid_for_passing_image_comparison(self):
        """Each image-typed comparison emits an <img> tag we can click on."""
        report = build_report(
            _make_suite_run(),
            comparisons=_make_comparisons(),
        )
        html = render_html(report)
        # Workflow name is wf_pass_0 and the test_file is out_0.png.
        assert "<img" in html
        assert "wf_pass_0/out_0.png" in html
        # No diff_artifact set on the passing comparison so no diff tile.
        assert "img-tile diff" not in html

    def test_image_grid_includes_diff_when_failed(self):
        """SSIM diff artifacts are surfaced as side-by-side overlay tiles."""
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="baseline/out_0.png",
                    test_file="out_0.png",
                    result=CompareResult(
                        method="ssim", score=0.5, passed=False,
                        threshold=0.95,
                        diff_artifact=Path("out_0_ssim_diff.png"),
                    ),
                ),
            ],
        }
        report = build_report(_make_suite_run(), comparisons=comparisons)
        html = render_html(report)
        assert "img-tile diff" in html
        assert "wf_pass_0/out_0_ssim_diff.png" in html

    def test_artifact_url_prefix_rewrites_img_src(self):
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="baseline/out_0.png",
                    test_file="out_0.png",
                    result=CompareResult(
                        method="ssim", score=0.5, passed=False,
                        threshold=0.95,
                        diff_artifact=Path("out_0_ssim_diff.png"),
                    ),
                ),
            ],
        }
        report = build_report(_make_suite_run(), comparisons=comparisons)
        html = render_html(
            report,
            artifact_url_prefix="/tests/T-abc/artifact",
        )
        assert "/tests/T-abc/artifact/wf_pass_0/out_0.png" in html
        assert "/tests/T-abc/artifact/wf_pass_0/out_0_ssim_diff.png" in html

    def test_non_image_comparisons_do_not_get_thumbnails(self):
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="baseline/data.txt",
                    test_file="data.txt",
                    result=CompareResult(
                        method="existence", passed=True,
                    ),
                ),
            ],
        }
        report = build_report(_make_suite_run(), comparisons=comparisons)
        html = render_html(report)
        # Only the comparison table -- no <img> tile for .txt.
        assert "<img" not in html

    def test_three_up_row_includes_baseline_tile(self):
        """A baseline tile is rendered alongside the test (new) tile."""
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="out_0.png",
                    test_file="out_0.png",
                    result=CompareResult(
                        method="ssim", score=0.5, passed=False,
                        threshold=0.95,
                        diff_artifact=Path("out_0_ssim_diff.png"),
                    ),
                ),
            ],
        }
        report = build_report(_make_suite_run(), comparisons=comparisons)
        html = render_html(report)
        # Baseline tile references the _baselines/<wf>/ directory.
        assert "img-tile baseline" in html
        assert "_baselines/wf_pass_0/out_0.png" in html
        # Test tile carries the fail modifier when comparison failed.
        assert "img-tile test fail" in html
        # All three (baseline + test + diff) are present.
        assert "img-tile diff" in html

    def test_three_up_row_test_passed_no_fail_class(self):
        """Passing comparisons render the test tile without the fail class."""
        report = build_report(
            _make_suite_run(), comparisons=_make_comparisons(),
        )
        html = render_html(report)
        assert "img-tile test fail" not in html
        # Baseline still rendered for passing comparisons (visual sanity).
        assert "img-tile baseline" in html

    def test_video_test_file_uses_video_tag(self):
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="clip.mp4",
                    test_file="clip.mp4",
                    result=CompareResult(
                        method="video_frame_ssim", score=0.5,
                        passed=False, threshold=0.9,
                        diff_artifact=Path("clip_frame_diff.png"),
                    ),
                ),
            ],
        }
        report = build_report(_make_suite_run(), comparisons=comparisons)
        html = render_html(report)
        assert "<video" in html
        assert "wf_pass_0/clip.mp4" in html
        # The frame-strip PNG diff still renders as <img>.
        assert "wf_pass_0/clip_frame_diff.png" in html


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

    def test_copies_baseline_image_for_html(self, tmp_path):
        """write_report copies baseline images into _baselines/<wf>/."""
        # Build a suite layout with a real baseline file on disk.
        suite_dir = tmp_path / "suite"
        wf_baseline_dir = suite_dir / "baselines" / "wf_pass_0"
        wf_baseline_dir.mkdir(parents=True)
        baseline_png = wf_baseline_dir / "out_0.png"
        baseline_png.write_bytes(b"\x89PNG\r\n\x1a\nfake-pixels")

        run = _make_suite_run()
        run.suite_path = suite_dir
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="out_0.png",
                    test_file="out_0.png",
                    result=CompareResult(
                        method="ssim", score=0.9, passed=True, threshold=0.95,
                    ),
                ),
            ],
        }
        report = build_report(run, comparisons=comparisons)
        out_dir = tmp_path / "run"
        write_report(report, out_dir, formats=["html"])

        copied = out_dir / "_baselines" / "wf_pass_0" / "out_0.png"
        assert copied.is_file()
        # File contents preserved (copy2 keeps bytes + mtime).
        assert copied.read_bytes() == baseline_png.read_bytes()

    def test_baseline_copy_skipped_without_suite_path(self, tmp_path):
        """No _baselines/ directory when report.suite_path is None."""
        run = _make_suite_run()
        run.suite_path = None
        report = build_report(run, comparisons=_make_comparisons())
        assert report.suite_path is None
        out_dir = tmp_path / "run"
        write_report(report, out_dir, formats=["html"])
        assert not (out_dir / "_baselines").exists()

    def test_baseline_copy_skipped_for_non_image(self, tmp_path):
        """Non-image baselines are not copied even when suite_path is set."""
        suite_dir = tmp_path / "suite"
        bl_dir = suite_dir / "baselines" / "wf_pass_0"
        bl_dir.mkdir(parents=True)
        (bl_dir / "data.txt").write_text("baseline")

        run = _make_suite_run()
        run.suite_path = suite_dir
        comparisons = {
            "wf_pass_0": [
                ComparisonEntry(
                    baseline_file="data.txt",
                    test_file="data.txt",
                    result=CompareResult(method="existence", passed=True),
                ),
            ],
        }
        report = build_report(run, comparisons=comparisons)
        out_dir = tmp_path / "run"
        write_report(report, out_dir, formats=["html"])
        assert not (out_dir / "_baselines" / "wf_pass_0" / "data.txt").exists()


# ---------------------------------------------------------------------------
# suite_path propagation
# ---------------------------------------------------------------------------

class TestSuitePathPropagation:
    def test_build_report_defaults_to_suite_run_suite_path(self):
        run = _make_suite_run()
        run.suite_path = Path("/some/suite/dir")
        report = build_report(run)
        assert report.suite_path == "/some/suite/dir" or report.suite_path == str(
            Path("/some/suite/dir")
        )

    def test_build_report_explicit_suite_path_wins(self):
        run = _make_suite_run()
        run.suite_path = Path("/from/run")
        report = build_report(run, suite_path=Path("/explicit"))
        assert report.suite_path == str(Path("/explicit"))

    def test_suite_path_in_json_roundtrip(self, tmp_path):
        run = _make_suite_run()
        run.suite_path = tmp_path / "suite"
        report = build_report(run)
        write_report(report, tmp_path, formats=["json"])
        data = json.loads((tmp_path / "report.json").read_text())
        assert data["suite_path"] == str(tmp_path / "suite")
