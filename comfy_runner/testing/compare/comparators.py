"""Built-in comparator implementations.

Heavy dependencies (Pillow, numpy) are lazy-imported so the base
package stays lightweight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .registry import CompareResult, register


# ---------------------------------------------------------------------------
# existence — verify output was produced (zero deps)
# ---------------------------------------------------------------------------

def _existence(baseline: Path, test: Path, **kwargs: Any) -> CompareResult:
    """Check that both baseline and test files exist and are non-empty."""
    if not test.is_file():
        return CompareResult(method="existence", passed=False,
                             details={"error": "test file missing"})
    if test.stat().st_size == 0:
        return CompareResult(method="existence", passed=False,
                             details={"error": "test file is empty"})
    return CompareResult(method="existence", passed=True,
                         details={"test_size": test.stat().st_size})

register("existence", _existence)


# ---------------------------------------------------------------------------
# file_size — compare file sizes within a tolerance
# ---------------------------------------------------------------------------

def _file_size(
    baseline: Path, test: Path, *, threshold: float = 0.10, **kwargs: Any,
) -> CompareResult:
    """Compare file sizes.  Passes if the relative difference is ≤ threshold."""
    if not test.is_file() or not baseline.is_file():
        return CompareResult(method="file_size", passed=False,
                             details={"error": "file missing"})

    b_size = baseline.stat().st_size
    t_size = test.stat().st_size

    if b_size == 0:
        passed = t_size == 0
        return CompareResult(method="file_size", score=1.0 if passed else 0.0,
                             passed=passed, threshold=threshold)

    ratio = abs(t_size - b_size) / b_size
    passed = ratio <= threshold
    return CompareResult(
        method="file_size",
        score=round(1.0 - ratio, 4),
        passed=passed,
        threshold=threshold,
        details={"baseline_size": b_size, "test_size": t_size, "ratio": round(ratio, 4)},
    )

register("file_size", _file_size)


# ---------------------------------------------------------------------------
# ssim — structural similarity (requires Pillow + numpy)
# ---------------------------------------------------------------------------

def _ssim(
    baseline: Path, test: Path, *, threshold: float = 0.95, **kwargs: Any,
) -> CompareResult:
    """Compute SSIM between two images.

    Requires ``Pillow`` and ``numpy``.  Raises ``ImportError`` if not installed.
    """
    import numpy as np
    from PIL import Image

    img_b = Image.open(baseline).convert("L")
    img_t = Image.open(test).convert("L")

    # Resize test to match baseline if dimensions differ
    if img_t.size != img_b.size:
        img_t = img_t.resize(img_b.size, Image.LANCZOS)

    arr_b = np.asarray(img_b, dtype=np.float64)
    arr_t = np.asarray(img_t, dtype=np.float64)

    score = _compute_ssim(arr_b, arr_t)
    passed = score >= threshold

    result = CompareResult(
        method="ssim",
        score=round(score, 4),
        passed=passed,
        threshold=threshold,
        details={"baseline_size": img_b.size, "test_size": Image.open(test).size},
    )

    # Generate diff heatmap if requested
    if kwargs.get("save_diff") and not passed:
        diff_path = test.parent / f"{test.stem}_ssim_diff.png"
        _save_ssim_diff(arr_b, arr_t, diff_path)
        result.diff_artifact = diff_path

    return result


def _compute_ssim(a: Any, b: Any, win_size: int = 7) -> float:
    """Compute mean SSIM between two 2D numpy arrays using local windows.

    Uses a sliding uniform window to compute local statistics, matching
    the standard SSIM definition (spatial structural comparison).
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2

    # If the image is too small for the window, fall back to global stats
    if a.shape[0] < win_size or a.shape[1] < win_size:
        mu_a = a.mean()
        mu_b = b.mean()
        sigma_a_sq = a.var()
        sigma_b_sq = b.var()
        sigma_ab = ((a - mu_a) * (b - mu_b)).mean()
        num = (2 * mu_a * mu_b + C1) * (2 * sigma_ab + C2)
        den = (mu_a ** 2 + mu_b ** 2 + C1) * (sigma_a_sq + sigma_b_sq + C2)
        return float(num / den)

    # Extract sliding windows
    patches_a = sliding_window_view(a, (win_size, win_size))
    patches_b = sliding_window_view(b, (win_size, win_size))

    mu_a = patches_a.mean(axis=(-2, -1))
    mu_b = patches_b.mean(axis=(-2, -1))
    sigma_a_sq = patches_a.var(axis=(-2, -1))
    sigma_b_sq = patches_b.var(axis=(-2, -1))
    sigma_ab = ((patches_a - mu_a[..., None, None]) *
                (patches_b - mu_b[..., None, None])).mean(axis=(-2, -1))

    numerator = (2 * mu_a * mu_b + C1) * (2 * sigma_ab + C2)
    denominator = (mu_a ** 2 + mu_b ** 2 + C1) * (sigma_a_sq + sigma_b_sq + C2)

    ssim_map = numerator / denominator
    return float(ssim_map.mean())


def _save_ssim_diff(a: Any, b: Any, path: Path) -> None:
    """Save a visual diff heatmap showing SSIM differences."""
    import numpy as np
    from PIL import Image

    diff = np.abs(a - b)
    # Normalize to 0-255
    if diff.max() > 0:
        diff = (diff / diff.max() * 255).astype(np.uint8)
    else:
        diff = diff.astype(np.uint8)
    Image.fromarray(diff).save(path)

register("ssim", _ssim)


# ---------------------------------------------------------------------------
# ahash — average hash distance (requires Pillow)
# ---------------------------------------------------------------------------

def _ahash(
    baseline: Path, test: Path, *, threshold: float = 0.90, **kwargs: Any,
) -> CompareResult:
    """Compare images using average hash (aHash).

    Resizes to 8x8 grayscale and compares each pixel to the mean.
    Score is ``1.0 - (hamming_distance / hash_bits)``.
    Requires ``Pillow``.
    """
    from PIL import Image

    hash_b = _compute_ahash(Image.open(baseline))
    hash_t = _compute_ahash(Image.open(test))

    distance = bin(hash_b ^ hash_t).count("1")
    hash_bits = 64  # 8x8 hash
    score = 1.0 - (distance / hash_bits)
    passed = score >= threshold

    return CompareResult(
        method="ahash",
        score=round(score, 4),
        passed=passed,
        threshold=threshold,
        details={"hamming_distance": distance, "hash_bits": hash_bits},
    )


def _compute_ahash(img: Any) -> int:
    """Compute a 64-bit average hash of an image.

    Resizes to 8x8 grayscale, compares each pixel to the mean brightness.
    """
    small = img.convert("L").resize((8, 8))
    pixels = list(small.tobytes())
    mean = sum(pixels) / len(pixels)
    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= mean else 0)
    return bits

register("ahash", _ahash)


# ---------------------------------------------------------------------------
# pixel_mse — mean squared error (requires Pillow + numpy)
# ---------------------------------------------------------------------------

def _pixel_mse(
    baseline: Path, test: Path, *, threshold: float = 0.95, **kwargs: Any,
) -> CompareResult:
    """Compare images using pixel-level mean squared error.

    Score is ``1.0 - (mse / max_mse)`` where max_mse = 255^2.
    Requires ``Pillow`` and ``numpy``.
    """
    import numpy as np
    from PIL import Image

    img_b = Image.open(baseline).convert("RGB")
    img_t = Image.open(test).convert("RGB")

    if img_t.size != img_b.size:
        img_t = img_t.resize(img_b.size, Image.LANCZOS)

    arr_b = np.asarray(img_b, dtype=np.float64)
    arr_t = np.asarray(img_t, dtype=np.float64)

    mse = float(np.mean((arr_b - arr_t) ** 2))
    max_mse = 255.0 ** 2
    score = 1.0 - (mse / max_mse)
    passed = score >= threshold

    return CompareResult(
        method="pixel_mse",
        score=round(score, 6),
        passed=passed,
        threshold=threshold,
        details={"mse": round(mse, 2), "max_mse": max_mse},
    )

register("pixel_mse", _pixel_mse)


# ---------------------------------------------------------------------------
# metadata — timing regression check (zero deps)
# ---------------------------------------------------------------------------

def _metadata(
    baseline: Path, test: Path, *, threshold: float = 0.20, **kwargs: Any,
) -> CompareResult:
    """Check for timing regressions by comparing metadata files.

    Expects JSON files with an ``execution_time`` field.
    Passes if test time is within (1 + threshold) of baseline time.
    """
    import json

    try:
        with open(baseline) as f:
            b_data = json.load(f)
        with open(test) as f:
            t_data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return CompareResult(method="metadata", passed=False,
                             details={"error": str(exc)})

    b_time = b_data.get("execution_time")
    t_time = t_data.get("execution_time")
    if b_time is None or t_time is None:
        return CompareResult(method="metadata", passed=True,
                             details={"note": "no execution_time to compare"})

    if b_time == 0:
        return CompareResult(method="metadata", passed=True,
                             details={"baseline_time": 0, "test_time": t_time})

    regression = (t_time - b_time) / b_time
    passed = regression <= threshold
    score = max(0.0, 1.0 - max(0.0, regression))

    return CompareResult(
        method="metadata",
        score=round(score, 4),
        passed=passed,
        threshold=threshold,
        details={
            "baseline_time": round(b_time, 2),
            "test_time": round(t_time, 2),
            "regression_pct": round(regression * 100, 1),
        },
    )

register("metadata", _metadata)


# ---------------------------------------------------------------------------
# video_frame_ssim — sample frames from a video and SSIM each pair
# ---------------------------------------------------------------------------

def _video_frame_ssim(
    baseline: Path,
    test: Path,
    *,
    threshold: float = 0.90,
    frame_count: int = 5,
    **kwargs: Any,
) -> CompareResult:
    """Sample ``frame_count`` frames from each video and SSIM-compare pairs.

    Score is the mean SSIM across all sampled frame pairs.  Passes when
    the mean meets ``threshold``.  On failure (and when ``save_diff`` is
    truthy) a single composite PNG strip is written next to *test*:
    rows = frame index, columns = baseline | test | per-pixel diff.

    Requires ``ffmpeg`` on ``PATH`` (used to extract frames) plus
    ``Pillow`` and ``numpy`` (already required for image SSIM).  Raises
    ``RuntimeError`` if ``ffmpeg`` is not available.
    """
    import shutil
    import tempfile

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "video_frame_ssim requires ffmpeg on PATH "
            "(install via your OS package manager or `imageio-ffmpeg`)"
        )

    import numpy as np
    from PIL import Image

    if not test.is_file() or not baseline.is_file():
        return CompareResult(method="video_frame_ssim", passed=False,
                             details={"error": "video file missing"})

    frame_count = max(1, int(frame_count))

    with tempfile.TemporaryDirectory(prefix="video_ssim_") as td:
        td_path = Path(td)
        b_dir = td_path / "baseline"
        t_dir = td_path / "test"
        b_dir.mkdir()
        t_dir.mkdir()

        # Sample N frames evenly spaced via ffmpeg's ``thumbnail`` is
        # quality-biased; we want a deterministic time grid, so use
        # ``select`` with frame indices computed from probed nb_frames.
        nb_b = _probe_frame_count(baseline)
        nb_t = _probe_frame_count(test)
        if nb_b == 0 or nb_t == 0:
            return CompareResult(
                method="video_frame_ssim", passed=False,
                details={"error": "could not probe video frame count"},
            )

        n = min(frame_count, nb_b, nb_t)
        b_indices = _evenly_spaced(nb_b, n)
        t_indices = _evenly_spaced(nb_t, n)

        for i, idx in enumerate(b_indices):
            _extract_frame(baseline, idx, b_dir / f"f{i:04d}.png")
        for i, idx in enumerate(t_indices):
            _extract_frame(test, idx, t_dir / f"f{i:04d}.png")

        # Compute per-frame SSIM.
        per_frame: list[float] = []
        pairs: list[tuple[np.ndarray, np.ndarray]] = []
        for i in range(n):
            bp = b_dir / f"f{i:04d}.png"
            tp = t_dir / f"f{i:04d}.png"
            if not bp.is_file() or not tp.is_file():
                continue
            img_b = Image.open(bp).convert("L")
            img_t = Image.open(tp).convert("L")
            if img_t.size != img_b.size:
                img_t = img_t.resize(img_b.size, Image.LANCZOS)
            arr_b = np.asarray(img_b, dtype=np.float64)
            arr_t = np.asarray(img_t, dtype=np.float64)
            per_frame.append(_compute_ssim(arr_b, arr_t))
            pairs.append((arr_b, arr_t))

        if not per_frame:
            return CompareResult(
                method="video_frame_ssim", passed=False,
                details={"error": "no extractable frames"},
            )

        mean_score = float(sum(per_frame) / len(per_frame))
        passed = mean_score >= threshold

        result = CompareResult(
            method="video_frame_ssim",
            score=round(mean_score, 4),
            passed=passed,
            threshold=threshold,
            details={
                "frames_compared": len(per_frame),
                "per_frame_ssim": [round(s, 4) for s in per_frame],
                "min_frame_ssim": round(min(per_frame), 4),
            },
        )

        if kwargs.get("save_diff") and not passed:
            diff_path = test.parent / f"{test.stem}_frame_diff.png"
            _save_video_strip_diff(pairs, per_frame, diff_path)
            result.diff_artifact = diff_path

        return result


def _probe_frame_count(path: Path) -> int:
    """Return the total frame count of *path* via ffprobe, or 0 on failure."""
    import subprocess
    import shutil

    if shutil.which("ffprobe") is None:
        # Fall back to ffmpeg packet count.
        return _probe_frame_count_via_ffmpeg(path)
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-count_packets",
                "-show_entries", "stream=nb_read_packets",
                "-of", "csv=p=0",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
        )
        return int(out.decode().strip() or 0)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return 0


def _probe_frame_count_via_ffmpeg(path: Path) -> int:
    """Fallback frame count via ``ffmpeg -vcodec copy -f null``."""
    import re
    import subprocess

    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostats", "-i", str(path),
             "-vcodec", "copy", "-f", "null", "-"],
            capture_output=True, text=True, check=False,
        )
        # ffmpeg writes ``frame= NNNN`` to stderr.
        m = re.search(r"frame=\s*(\d+)", proc.stderr)
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def _evenly_spaced(total: int, n: int) -> list[int]:
    """Return ``n`` 0-based frame indices evenly spaced across ``total``."""
    if n <= 1:
        return [max(0, total // 2)]
    step = (total - 1) / (n - 1)
    return [int(round(i * step)) for i in range(n)]


def _extract_frame(video: Path, index: int, out: Path) -> None:
    """Extract the frame at zero-based ``index`` from *video* to *out*."""
    import subprocess

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(video),
                "-vf", f"select=eq(n\\,{index})",
                "-frames:v", "1",
                str(out),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _save_video_strip_diff(
    pairs: list[tuple[Any, Any]],
    per_frame_ssim: list[float],
    path: Path,
) -> None:
    """Write a composite PNG: one row per frame, columns = baseline | test | diff.

    Per-frame SSIM is rendered as a label band underneath each row.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    if not pairs:
        return

    # Use the first pair's shape as the canonical tile size.
    h, w = pairs[0][0].shape[:2]
    cols = 3
    label_h = 18
    row_h = h + label_h
    canvas = Image.new("RGB", (w * cols, row_h * len(pairs)), color=(11, 18, 32))
    draw = ImageDraw.Draw(canvas)

    for i, ((arr_b, arr_t), score) in enumerate(zip(pairs, per_frame_ssim)):
        # Normalize sizes to the canonical tile.
        b_img = Image.fromarray(arr_b.astype(np.uint8)).convert("RGB").resize((w, h))
        t_img = Image.fromarray(arr_t.astype(np.uint8)).convert("RGB").resize((w, h))
        diff_arr = np.abs(arr_b - arr_t)
        if diff_arr.max() > 0:
            diff_arr = (diff_arr / diff_arr.max() * 255).astype(np.uint8)
        else:
            diff_arr = diff_arr.astype(np.uint8)
        d_img = Image.fromarray(diff_arr).convert("RGB").resize((w, h))

        y = i * row_h
        canvas.paste(b_img, (0, y))
        canvas.paste(t_img, (w, y))
        canvas.paste(d_img, (w * 2, y))
        draw.text((4, y + h + 2),
                  f"frame {i}  ssim={score:.4f}",
                  fill=(226, 232, 240))

    canvas.save(path)


register("video_frame_ssim", _video_frame_ssim)
