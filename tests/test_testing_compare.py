"""Tests for comfy_runner.testing.compare — comparator registry and built-ins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_runner.testing.compare import (
    REGISTRY,
    CompareResult,
    compare_outputs,
    get_comparator,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_builtin_comparators_registered(self):
        expected = {"existence", "file_size", "ssim", "ahash", "pixel_mse", "metadata"}
        assert expected.issubset(set(REGISTRY.keys()))

    def test_get_comparator_exists(self):
        fn = get_comparator("existence")
        assert callable(fn)

    def test_get_comparator_missing(self):
        with pytest.raises(KeyError, match="Unknown comparator"):
            get_comparator("nonexistent_method")


# ---------------------------------------------------------------------------
# existence
# ---------------------------------------------------------------------------

class TestExistence:
    def test_passes_for_existing_file(self, tmp_path):
        baseline = tmp_path / "baseline.png"
        test = tmp_path / "test.png"
        baseline.write_bytes(b"baseline data")
        test.write_bytes(b"test data")
        result = compare_outputs(baseline, test, {"method": "existence"})
        assert result.passed is True
        assert result.method == "existence"

    def test_fails_for_missing_test(self, tmp_path):
        baseline = tmp_path / "baseline.png"
        test = tmp_path / "test.png"
        baseline.write_bytes(b"baseline data")
        result = compare_outputs(baseline, test, {"method": "existence"})
        assert result.passed is False

    def test_fails_for_empty_test(self, tmp_path):
        baseline = tmp_path / "baseline.png"
        test = tmp_path / "test.png"
        baseline.write_bytes(b"baseline data")
        test.write_bytes(b"")
        result = compare_outputs(baseline, test, {"method": "existence"})
        assert result.passed is False

    def test_default_fallback(self, tmp_path):
        baseline = tmp_path / "baseline.png"
        test = tmp_path / "test.png"
        baseline.write_bytes(b"data")
        test.write_bytes(b"data")
        result = compare_outputs(baseline, test)  # no config = existence
        assert result.passed is True


# ---------------------------------------------------------------------------
# file_size
# ---------------------------------------------------------------------------

class TestFileSize:
    def test_same_size(self, tmp_path):
        baseline = tmp_path / "b.bin"
        test = tmp_path / "t.bin"
        baseline.write_bytes(b"x" * 100)
        test.write_bytes(b"y" * 100)
        result = compare_outputs(baseline, test, {"method": "file_size", "threshold": 0.10})
        assert result.passed is True
        assert result.score == 1.0

    def test_within_threshold(self, tmp_path):
        baseline = tmp_path / "b.bin"
        test = tmp_path / "t.bin"
        baseline.write_bytes(b"x" * 100)
        test.write_bytes(b"y" * 105)  # 5% difference
        result = compare_outputs(baseline, test, {"method": "file_size", "threshold": 0.10})
        assert result.passed is True

    def test_exceeds_threshold(self, tmp_path):
        baseline = tmp_path / "b.bin"
        test = tmp_path / "t.bin"
        baseline.write_bytes(b"x" * 100)
        test.write_bytes(b"y" * 200)  # 100% difference
        result = compare_outputs(baseline, test, {"method": "file_size", "threshold": 0.10})
        assert result.passed is False

    def test_missing_file(self, tmp_path):
        baseline = tmp_path / "b.bin"
        test = tmp_path / "t.bin"
        baseline.write_bytes(b"x" * 100)
        result = compare_outputs(baseline, test, {"method": "file_size"})
        assert result.passed is False


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_no_regression(self, tmp_path):
        baseline = tmp_path / "b.json"
        test = tmp_path / "t.json"
        baseline.write_text(json.dumps({"execution_time": 10.0}))
        test.write_text(json.dumps({"execution_time": 10.5}))
        result = compare_outputs(baseline, test, {"method": "metadata", "threshold": 0.20})
        assert result.passed is True

    def test_regression_detected(self, tmp_path):
        baseline = tmp_path / "b.json"
        test = tmp_path / "t.json"
        baseline.write_text(json.dumps({"execution_time": 10.0}))
        test.write_text(json.dumps({"execution_time": 15.0}))  # 50% slower
        result = compare_outputs(baseline, test, {"method": "metadata", "threshold": 0.20})
        assert result.passed is False
        assert result.details["regression_pct"] == 50.0

    def test_no_execution_time(self, tmp_path):
        baseline = tmp_path / "b.json"
        test = tmp_path / "t.json"
        baseline.write_text(json.dumps({"other": "data"}))
        test.write_text(json.dumps({"other": "data"}))
        result = compare_outputs(baseline, test, {"method": "metadata"})
        assert result.passed is True

    def test_invalid_json(self, tmp_path):
        baseline = tmp_path / "b.json"
        test = tmp_path / "t.json"
        baseline.write_text("not json")
        test.write_text(json.dumps({"execution_time": 10.0}))
        result = compare_outputs(baseline, test, {"method": "metadata"})
        assert result.passed is False


# ---------------------------------------------------------------------------
# ssim (requires Pillow + numpy — skip if not available)
# ---------------------------------------------------------------------------

_has_pillow_numpy = True
try:
    import numpy  # noqa: F401
    from PIL import Image  # noqa: F401
except ImportError:
    _has_pillow_numpy = False

needs_pillow = pytest.mark.skipif(not _has_pillow_numpy, reason="Pillow/numpy not installed")


def _make_image(path: Path, size: tuple[int, int] = (64, 64), color: int = 128) -> Path:
    """Create a simple grayscale PNG for testing."""
    from PIL import Image
    img = Image.new("L", size, color)
    img.save(path)
    return path


@needs_pillow
class TestSSIM:
    def test_identical_images(self, tmp_path):
        baseline = _make_image(tmp_path / "b.png", color=128)
        test = _make_image(tmp_path / "t.png", color=128)
        result = compare_outputs(baseline, test, {"method": "ssim", "threshold": 0.95})
        assert result.passed is True
        assert result.score == 1.0

    def test_different_images(self, tmp_path):
        baseline = _make_image(tmp_path / "b.png", color=0)
        test = _make_image(tmp_path / "t.png", color=255)
        result = compare_outputs(baseline, test, {"method": "ssim", "threshold": 0.95})
        assert result.passed is False
        assert result.score is not None and result.score < 0.5

    def test_different_sizes_resized(self, tmp_path):
        baseline = _make_image(tmp_path / "b.png", size=(64, 64), color=128)
        test = _make_image(tmp_path / "t.png", size=(128, 128), color=128)
        result = compare_outputs(baseline, test, {"method": "ssim", "threshold": 0.95})
        assert result.passed is True


@needs_pillow
class TestAHash:
    def test_identical_images(self, tmp_path):
        baseline = _make_image(tmp_path / "b.png", color=100)
        test = _make_image(tmp_path / "t.png", color=100)
        result = compare_outputs(baseline, test, {"method": "ahash", "threshold": 0.90})
        assert result.passed is True
        assert result.score == 1.0
        assert result.details["hamming_distance"] == 0

    def test_different_images(self, tmp_path):
        import numpy as np
        from PIL import Image
        # Create a gradient image and its inverse — these produce different hashes
        arr = np.tile(np.arange(64, dtype=np.uint8) * 4, (64, 1))
        Image.fromarray(arr, "L").save(tmp_path / "b.png")
        Image.fromarray(255 - arr, "L").save(tmp_path / "t.png")
        result = compare_outputs(tmp_path / "b.png", tmp_path / "t.png",
                                 {"method": "ahash", "threshold": 0.90})
        assert result.details["hamming_distance"] > 0


@needs_pillow
class TestPixelMSE:
    def test_identical_images(self, tmp_path):
        baseline = _make_image(tmp_path / "b.png", color=128)
        test = _make_image(tmp_path / "t.png", color=128)
        result = compare_outputs(baseline, test, {"method": "pixel_mse", "threshold": 0.95})
        assert result.passed is True
        assert result.score == 1.0
        assert result.details["mse"] == 0.0

    def test_different_images(self, tmp_path):
        baseline = _make_image(tmp_path / "b.png", color=0)
        test = _make_image(tmp_path / "t.png", color=255)
        result = compare_outputs(baseline, test, {"method": "pixel_mse", "threshold": 0.95})
        assert result.passed is False
        assert result.details["mse"] > 0


# ---------------------------------------------------------------------------
# compare_outputs dispatch
# ---------------------------------------------------------------------------

class TestCompareOutputsDispatch:
    def test_passes_extra_kwargs(self, tmp_path):
        baseline = tmp_path / "b.bin"
        test = tmp_path / "t.bin"
        baseline.write_bytes(b"x" * 100)
        test.write_bytes(b"y" * 100)
        result = compare_outputs(baseline, test, {
            "method": "file_size",
            "threshold": 0.5,
        })
        assert result.threshold == 0.5
