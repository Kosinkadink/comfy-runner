"""Environment setup — download standalone env, create venv, copy site-packages.

Mirrors ComfyUI-Launcher pythonEnv.ts: createEnv, getUvPath,
getMasterPythonPath, getActivePythonPath, findSitePackages.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Callable

import requests

from .config import get_github_token

DL_META_SUFFIX = ".dl-meta"

RELEASE_REPO = "Comfy-Org/ComfyUI-Standalone-Environments"
VENV_DIR = Path("ComfyUI") / ".venv"

# Legacy layout constants (for migration / fallback)
_LEGACY_ENVS_DIR = "envs"
_LEGACY_DEFAULT_ENV = "default"

PLATFORM_PREFIX: dict[str, str] = {
    "Windows": "win-",
    "Darwin": "mac-",
    "Linux": "linux-",
}

VARIANT_LABELS: dict[str, str] = {
    "nvidia": "NVIDIA",
    "intel-xpu": "Intel Arc (XPU)",
    "amd": "AMD",
    "cpu": "CPU",
    "mps": "Apple Silicon (MPS)",
}


# ---------------------------------------------------------------------------
# Path helpers — mirror pythonEnv.ts
# ---------------------------------------------------------------------------

def get_uv_path(install_path: str | Path) -> Path:
    """Path to the uv binary inside the standalone env."""
    base = Path(install_path) / "standalone-env"
    if sys.platform == "win32":
        return base / "uv.exe"
    return base / "bin" / "uv"


def get_master_python_path(install_path: str | Path) -> Path:
    """Path to the master Python inside the standalone env."""
    base = Path(install_path) / "standalone-env"
    if sys.platform == "win32":
        return base / "python.exe"
    return base / "bin" / "python3"


def get_venv_dir(install_path: str | Path) -> Path:
    """Path to the single venv directory: ``<install>/ComfyUI/.venv``."""
    return Path(install_path) / VENV_DIR


def get_active_venv_dir(install_path: str | Path) -> Path | None:
    """Resolve the active venv root directory.

    Checks ``ComfyUI/.venv`` first, then falls back to the legacy
    ``envs/default`` layout.  Returns *None* if neither exists.
    """
    install_path = Path(install_path)
    venv = install_path / VENV_DIR
    if venv.exists():
        return venv
    legacy = install_path / _LEGACY_ENVS_DIR / _LEGACY_DEFAULT_ENV
    if legacy.exists():
        return legacy
    return None


def get_active_python_path(install_path: str | Path) -> Path | None:
    """Resolve the active Python binary.

    Checks ``ComfyUI/.venv`` first, then falls back to the legacy
    ``envs/default`` layout.  Returns *None* if neither exists.
    """
    env_dir = get_active_venv_dir(install_path)
    if env_dir is None:
        return None
    if sys.platform == "win32":
        py = env_dir / "Scripts" / "python.exe"
    else:
        py = env_dir / "bin" / "python3"
    return py if py.exists() else None


def find_site_packages(env_root: str | Path) -> Path | None:
    """Locate the site-packages directory within an env root."""
    root = Path(env_root)
    if sys.platform == "win32":
        sp = root / "Lib" / "site-packages"
        return sp if sp.exists() else None
    lib_dir = root / "lib"
    if lib_dir.exists():
        for entry in lib_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("python"):
                sp = entry / "site-packages"
                if sp.exists():
                    return sp
    return None


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

def _run_silent(cmd: list[str], timeout: int = 5) -> subprocess.CompletedProcess[bytes] | None:
    """Run a command silently, returning the result or None on failure."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _has_nvidia_smi() -> bool:
    result = _run_silent(["nvidia-smi"])
    return result is not None and result.returncode == 0


def _has_rocm_smi() -> bool:
    """Check if rocm-smi is present AND reports at least one GPU."""
    result = _run_silent(["rocm-smi"])
    if result is None or result.returncode != 0:
        return False
    # rocm-smi succeeds but might report no GPUs
    stdout = result.stdout.decode("utf-8", errors="replace")
    # If output contains "No AMD GPUs" or is essentially empty, it's not a real GPU
    if "no amd" in stdout.lower() or "no gpus" in stdout.lower():
        return False
    return True


def _has_xpu_smi() -> bool:
    """Check if xpu-smi is present (Intel XPU tooling)."""
    result = _run_silent(["xpu-smi", "discovery"])
    return result is not None and result.returncode == 0


def _has_sycl_ls() -> bool:
    """Check if sycl-ls reports an Intel Level Zero GPU device.

    sycl-ls also lists Intel CPUs as OpenCL devices, so we must check
    specifically for Level Zero GPU entries (e.g. ``ext_oneapi_level_zero:gpu``).
    """
    result = _run_silent(["sycl-ls"], timeout=10)
    if result is None or result.returncode != 0:
        return False
    stdout = result.stdout.decode("utf-8", errors="replace").lower()
    return "ext_oneapi_level_zero:gpu" in stdout or ("level_zero" in stdout and "gpu" in stdout)


def detect_gpu() -> str:
    """Best-effort GPU detection. Returns nvidia/amd/intel/mps/cpu.

    Detection priority:
      1. NVIDIA (nvidia-smi)
      2. AMD discrete (rocm-smi confirms a real GPU)
      3. Intel XPU (xpu-smi or sycl-ls)
      4. CPU fallback
    """
    system = platform.system()
    if system == "Darwin":
        return "mps"

    # NVIDIA always wins
    if _has_nvidia_smi():
        return "nvidia"

    has_rocm = _has_rocm_smi()
    has_xpu = _has_xpu_smi() or _has_sycl_ls()

    # If rocm-smi confirms a real AMD discrete GPU, prefer AMD
    if has_rocm:
        return "amd"

    # Intel XPU detected (even if AMD iGPU vendor ID exists, no rocm = no real AMD)
    if has_xpu:
        return "intel"

    return "cpu"


def get_platform_prefix() -> str:
    """Return the platform prefix for variant matching."""
    system = platform.system()
    prefix = PLATFORM_PREFIX.get(system)
    if not prefix:
        raise RuntimeError(f"Unsupported platform: {system}")
    return prefix


def recommend_variant(variant_id: str, gpu: str) -> bool:
    """Check if a variant matches the detected GPU. Mirrors standalone.ts."""
    stripped = _strip_platform(variant_id)
    if gpu == "nvidia":
        return stripped == "nvidia" or stripped.startswith("nvidia-")
    if gpu == "amd":
        return stripped == "amd" or stripped.startswith("amd-")
    if gpu == "mps":
        return stripped == "mps" or stripped.startswith("mps-")
    if gpu == "intel":
        return stripped == "intel-xpu" or stripped.startswith("intel-xpu-")
    return stripped == "cpu"


def get_variant_label(variant_id: str) -> str:
    """Human-readable label for a variant. Mirrors standalone.ts."""
    stripped = _strip_platform(variant_id)
    if stripped in VARIANT_LABELS:
        return VARIANT_LABELS[stripped]
    for key, label in VARIANT_LABELS.items():
        if stripped == key or stripped.startswith(key + "-"):
            suffix = stripped[len(key) + 1:]
            return f"{label} ({suffix.upper()})" if suffix else label
    return stripped


def _strip_platform(variant_id: str) -> str:
    """Remove platform prefix from variant ID."""
    for prefix in ("win-", "mac-", "linux-"):
        if variant_id.startswith(prefix):
            return variant_id[len(prefix):]
    return variant_id


# ---------------------------------------------------------------------------
# Release fetching
# ---------------------------------------------------------------------------

def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "comfy-runner",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_releases(limit: int = 30) -> list[dict[str, Any]]:
    """Fetch recent releases from the standalone environments repo."""
    url = f"https://api.github.com/repos/{RELEASE_REPO}/releases?per_page={limit}"
    resp = requests.get(url, headers=_github_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_latest_release() -> dict[str, Any]:
    """Fetch the latest release."""
    url = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
    resp = requests.get(url, headers=_github_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_manifests(release: dict[str, Any]) -> list[dict[str, Any]]:
    """Download and parse manifests.json from a release."""
    asset = next(
        (a for a in release.get("assets", []) if a["name"] == "manifests.json"),
        None,
    )
    if not asset:
        raise RuntimeError(
            f"Release {release.get('tag_name', '?')} has no manifests.json asset"
        )
    resp = requests.get(
        asset["browser_download_url"],
        headers=_github_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def pick_variant(
    manifests: list[dict[str, Any]],
    release: dict[str, Any],
    variant_id: str | None = None,
    gpu: str | None = None,
) -> dict[str, Any]:
    """Select a variant from the manifests.

    Returns a dict with keys: manifest, variant_id, download_files.
    """
    prefix = get_platform_prefix()
    platform_manifests = [m for m in manifests if m["id"].startswith(prefix)]

    if not platform_manifests:
        raise RuntimeError(
            f"No variants found for platform prefix '{prefix}'. "
            f"Available: {[m['id'] for m in manifests]}"
        )

    if variant_id:
        # Exact match
        match = next((m for m in platform_manifests if m["id"] == variant_id), None)
        if not match:
            raise RuntimeError(
                f"Variant '{variant_id}' not found. "
                f"Available: {[m['id'] for m in platform_manifests]}"
            )
        manifest = match
    else:
        # Auto-detect
        detected = gpu or detect_gpu()
        recommended = [m for m in platform_manifests if recommend_variant(m["id"], detected)]
        if recommended:
            manifest = recommended[0]
        else:
            raise RuntimeError(
                f"No variant matches GPU '{detected}'. "
                f"Available: {[m['id'] for m in platform_manifests]}. "
                "Use --variant to specify one explicitly."
            )

    # Resolve download files from the release assets
    files_list = manifest.get("files", [])
    assets = release.get("assets", [])
    download_files = []
    for fname in files_list:
        asset = next((a for a in assets if a["name"] == fname), None)
        if asset:
            download_files.append({
                "url": asset["browser_download_url"],
                "filename": asset["name"],
                "size": asset["size"],
            })

    if not download_files:
        raise RuntimeError(
            f"No downloadable assets found for variant '{manifest['id']}'"
        )

    return {
        "manifest": manifest,
        "variant_id": manifest["id"],
        "download_files": download_files,
    }


# ---------------------------------------------------------------------------
# Download + extract (with cache and resume support)
# ---------------------------------------------------------------------------

def download_and_extract(
    download_files: list[dict[str, Any]],
    dest: Path,
    cache_key: str | None = None,
    send_output: Callable[[str], None] | None = None,
    max_cache_entries: int | None = None,
) -> None:
    """Download release archive(s) and extract to dest.

    Mirrors ComfyUI-Launcher installer.ts downloadAndExtractMulti:
    - Archives are cached in ~/.comfy-runner/cache/{cache_key}/
    - Downloads are resumable via .dl-meta sidecar files
    - File sizes are validated post-download
    - Handles multi-part 7z (.001) and tar.gz archives
    """
    from . import cache as download_cache

    dest.mkdir(parents=True, exist_ok=True)

    # Determine cache directory
    if cache_key:
        cache_dir = download_cache.get_cache_path(cache_key)
    else:
        cache_dir = download_cache.get_cache_path("_uncached")

    total_bytes = sum(f.get("size", 0) for f in download_files)
    completed_bytes = 0
    overall_start = time.monotonic()
    all_cached = True

    for i, finfo in enumerate(download_files, 1):
        url = finfo["url"]
        filename = finfo["filename"]
        expected_size = finfo.get("size", 0)
        file_path = cache_dir / filename
        file_label = f" ({i}/{len(download_files)})" if len(download_files) > 1 else ""

        if _is_download_complete(file_path, expected_size):
            # Already cached and valid
            completed_bytes += expected_size
            if send_output:
                send_output(f"Using cached {filename}{file_label}\n")
        else:
            all_cached = False
            if send_output:
                size_mb = expected_size / 1048576 if expected_size else 0
                send_output(f"Downloading {filename}{file_label} ({size_mb:.0f} MB)...\n")

            _download_file_resumable(
                url, file_path, expected_size,
                base_completed=completed_bytes,
                total_bytes=total_bytes,
                overall_start=overall_start,
                send_output=send_output,
            )
            completed_bytes += expected_size

    if cache_key:
        download_cache.touch(cache_key)
        if not all_cached:
            evict_kwargs: dict[str, Any] = {}
            if max_cache_entries is not None:
                evict_kwargs["max_entries"] = max_cache_entries
            download_cache.evict(**evict_kwargs)

    # Determine which file to extract
    cached_files = [cache_dir / f["filename"] for f in download_files]
    if len(cached_files) == 1:
        extract_file = cached_files[0]
    else:
        sorted_files = sorted(cached_files, key=lambda p: p.name)
        extract_file = next(
            (f for f in sorted_files if f.name.endswith(".001")),
            sorted_files[0],
        )

    if send_output:
        send_output(f"Extracting to {dest}...\n")

    _extract_archive(extract_file, dest, send_output)


def _is_download_complete(file_path: Path, expected_size: int = 0) -> bool:
    """Check if a download is complete (file exists, no .dl-meta sidecar).

    Mirrors ComfyUI-Launcher download.ts isDownloadComplete.
    """
    if not file_path.exists():
        return False
    meta_path = Path(str(file_path) + DL_META_SUFFIX)
    if meta_path.exists():
        return False
    if expected_size > 0:
        try:
            return file_path.stat().st_size == expected_size
        except OSError:
            return False
    return True


def _read_dl_meta(file_path: Path) -> dict[str, Any] | None:
    """Read .dl-meta sidecar for a download."""
    meta_path = Path(str(file_path) + DL_META_SUFFIX)
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_dl_meta(file_path: Path, meta: dict[str, Any]) -> None:
    """Write .dl-meta sidecar."""
    from safe_file import atomic_write
    meta_path = Path(str(file_path) + DL_META_SUFFIX)
    try:
        atomic_write(meta_path, json.dumps(meta))
    except OSError:
        pass


def _delete_dl_meta(file_path: Path) -> None:
    """Delete .dl-meta sidecar to mark download as complete."""
    meta_path = Path(str(file_path) + DL_META_SUFFIX)
    try:
        meta_path.unlink(missing_ok=True)
    except OSError:
        pass


def _format_time(secs: float) -> str:
    """Format seconds into human-readable time string."""
    if secs < 0:
        return "—"
    m, s = divmod(int(secs), 60)
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _download_file_resumable(
    url: str,
    dest: Path,
    expected_size: int = 0,
    base_completed: int = 0,
    total_bytes: int = 0,
    overall_start: float = 0.0,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Download a single file with resume support, size validation, and speed/ETA.

    Mirrors ComfyUI-Launcher download.ts:
    - Writes .dl-meta sidecar while in progress
    - Resumes via HTTP Range + If-Range headers
    - Validates file size post-download
    - On interrupt: keeps partial file + meta for resume
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    headers = _github_headers()
    headers.pop("Accept", None)
    headers.pop("X-GitHub-Api-Version", None)

    # Check for resumable partial download
    resume_from = 0
    existing_meta = _read_dl_meta(dest)
    if existing_meta and dest.exists():
        if existing_meta.get("url") == url:
            try:
                resume_from = dest.stat().st_size
            except OSError:
                resume_from = 0

            if resume_from == 0:
                dest.unlink(missing_ok=True)
                _delete_dl_meta(dest)
            elif expected_size > 0 and resume_from >= expected_size:
                # Already fully downloaded, meta just wasn't cleaned up
                _delete_dl_meta(dest)
                return
        else:
            # URL mismatch — start fresh
            dest.unlink(missing_ok=True)
            _delete_dl_meta(dest)
    elif not dest.exists():
        _delete_dl_meta(dest)

    if resume_from > 0 and existing_meta:
        etag = existing_meta.get("etag")
        headers["Range"] = f"bytes={resume_from}-"
        if etag:
            headers["If-Range"] = etag
        if send_output:
            send_output(f"  Resuming from {resume_from // 1048576} MB...\n")

    with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
        resp.raise_for_status()

        is_resumed = resp.status_code == 206 and resume_from > 0
        if is_resumed:
            base_bytes = resume_from
        else:
            # Server doesn't support resume or sent full file — start fresh
            if resume_from > 0:
                dest.unlink(missing_ok=True)
            base_bytes = 0

        content_length = int(resp.headers.get("content-length", 0))
        file_total = base_bytes + content_length if is_resumed else content_length
        effective_size = expected_size or file_total

        # Write meta to mark download as in-progress
        etag = resp.headers.get("etag")
        _write_dl_meta(dest, {
            "url": url,
            "expected_size": effective_size,
            "etag": etag,
        })

        received_bytes = base_bytes
        start_time = time.monotonic()
        chunk_size = 1024 * 1024  # 1 MB
        mode = "ab" if is_resumed else "wb"

        with open(dest, mode) as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
                received_bytes += len(chunk)

                if send_output and effective_size > 0:
                    elapsed = time.monotonic() - start_time
                    new_bytes = received_bytes - base_bytes
                    speed = new_bytes / 1048576 / elapsed if elapsed > 0 else 0

                    # Overall progress across all files
                    overall_received = base_completed + received_bytes
                    overall_elapsed = time.monotonic() - overall_start if overall_start else elapsed
                    overall_speed = overall_received / 1048576 / overall_elapsed if overall_elapsed > 0 else 0
                    remaining = total_bytes - overall_received if total_bytes else effective_size - received_bytes
                    eta = remaining / 1048576 / overall_speed if overall_speed > 0 and remaining > 0 else -1

                    pct = received_bytes * 100 // effective_size
                    send_output(
                        f"\r  {received_bytes // 1048576} / {effective_size // 1048576} MB"
                        f"  ·  {speed:.1f} MB/s"
                        f"  ·  {_format_time(elapsed)} elapsed"
                        f"  ·  {_format_time(eta)} remaining"
                    )

    if send_output:
        send_output("\n")

    # Validate file size
    if effective_size > 0:
        try:
            actual = dest.stat().st_size
        except OSError as e:
            dest.unlink(missing_ok=True)
            _delete_dl_meta(dest)
            raise RuntimeError(f"Cannot stat downloaded file: {e}") from e

        if actual != effective_size:
            dest.unlink(missing_ok=True)
            _delete_dl_meta(dest)
            raise RuntimeError(
                f"Download incomplete: expected {effective_size} bytes "
                f"but got {actual}"
            )

    # Mark download as complete by removing meta
    _delete_dl_meta(dest)


def _safe_tar_extractall(
    tar: tarfile.TarFile,
    dest: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Extract tar with data filter when available (Python 3.12+), plain otherwise."""
    members = tar.getmembers()
    total = len(members)
    for i, member in enumerate(members, 1):
        if sys.version_info >= (3, 12):
            tar.extract(member, dest, filter="data")
        else:
            tar.extract(member, dest)
        if send_output and total > 0 and i % 500 == 0:
            pct = i * 100 // total
            send_output(f"\r  Extracted {i}/{total} files ({pct}%)")
    if send_output and total > 0:
        send_output(f"\r  Extracted {total}/{total} files (100%)\n")


def _find_7z() -> str | None:
    """Locate native 7z executable. Returns path string, or None if not found."""
    found = shutil.which("7z")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "7-Zip" / "7z.exe",
            Path(os.environ.get("LocalAppData", "")) / "Programs" / "7-Zip" / "7z.exe",
        ):
            if candidate.exists():
                return str(candidate)
    return None


def _extract_7z_native(
    seven_zip: str,
    archive_path: Path,
    dest: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Extract using native 7z subprocess with real-time progress output."""
    cmd = [seven_zip, "x", str(archive_path), f"-o{dest}", "-y", "-bsp1"]
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )
    assert proc.stdout is not None
    buf = ""
    for ch in iter(lambda: proc.stdout.read(1), ""):
        if ch in ("\r", "\n"):
            line = buf.strip()
            if line and send_output:
                # 7z progress lines look like " 42% - filename"
                if line[0].isdigit() or line.startswith(" "):
                    send_output(f"\r  7z: {line}")
                else:
                    send_output(f"  {line}\n")
            buf = ""
        else:
            buf += ch
    proc.wait()
    if proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(
            f"7z extraction failed (exit {proc.returncode}): {stderr}"
        )
    if send_output:
        send_output("\n")


class _ExtractProgress:
    """py7zr ExtractCallback that reports progress via send_output.

    py7zr fires report_update with *delta* decompressed bytes (~every 1s),
    then report_end with the file's *total* uncompressed size.  We track
    progress via report_update deltas and snap to the exact file total on
    report_end to avoid drift.
    """

    def __init__(
        self,
        total_bytes: int,
        send_output: Callable[[str], None],
    ) -> None:
        self._total = total_bytes
        self._send_output = send_output
        self._extracted = 0
        self._file_extracted = 0  # bytes tracked via report_update for current file
        self._file_count = 0
        self._start = time.monotonic()

    def report_start_preparation(self) -> None:
        pass

    def report_start(self, processing_file_path: str, processing_bytes: str) -> None:
        self._file_count += 1
        self._file_extracted = 0

    def report_update(self, decompressed_bytes: str) -> None:
        delta = int(decompressed_bytes)
        self._extracted += delta
        self._file_extracted += delta
        self._report()

    def report_end(self, processing_file_path: str, wrote_bytes: str) -> None:
        # Snap to exact file size to correct any drift from update deltas
        file_total = int(wrote_bytes)
        self._extracted += file_total - self._file_extracted
        self._file_extracted = 0
        self._report()

    def report_postprocess(self) -> None:
        pass

    def report_warning(self, message: str) -> None:
        self._send_output(f"  Warning: {message}\n")

    def _report(self) -> None:
        elapsed = time.monotonic() - self._start
        speed = self._extracted / 1048576 / elapsed if elapsed > 0 else 0
        if self._total > 0:
            pct = min(self._extracted * 100 // self._total, 100)
            remaining = self._total - self._extracted
            eta = remaining / 1048576 / speed if speed > 0 and remaining > 0 else -1
            self._send_output(
                f"\r  {pct}%  ·  {self._extracted // 1048576} / {self._total // 1048576} MB"
                f"  ·  {speed:.1f} MB/s"
                f"  ·  {_format_time(eta)} remaining"
                f"  ·  {self._file_count} files"
            )
        else:
            self._send_output(
                f"\r  {self._extracted // 1048576} MB extracted"
                f"  ·  {speed:.1f} MB/s"
                f"  ·  {self._file_count} files"
            )


def _make_extract_callback(
    total_bytes: int,
    send_output: Callable[[str], None],
) -> Any:
    """Create a py7zr ExtractCallback subclass instance for progress reporting.

    Built dynamically so py7zr is only imported when actually needed.
    """
    import py7zr.callbacks

    class _Cb(py7zr.callbacks.ExtractCallback):
        def __init__(self) -> None:
            self._p = _ExtractProgress(total_bytes, send_output)

        def report_start_preparation(self) -> None:
            self._p.report_start_preparation()

        def report_start(self, processing_file_path: str, processing_bytes: str) -> None:
            self._p.report_start(processing_file_path, processing_bytes)

        def report_update(self, decompressed_bytes: str) -> None:
            self._p.report_update(decompressed_bytes)

        def report_end(self, processing_file_path: str, wrote_bytes: str) -> None:
            self._p.report_end(processing_file_path, wrote_bytes)

        def report_postprocess(self) -> None:
            self._p.report_postprocess()

        def report_warning(self, message: str) -> None:
            self._p.report_warning(message)

    return _Cb()


def _extract_7z_py7zr(
    archive_path: Path,
    dest: Path,
    send_output: Callable[[str], None] | None = None,
    *,
    multivolume: bool = False,
) -> None:
    """Extract .7z or multi-volume .001 archive using py7zr with progress."""
    import py7zr

    if multivolume:
        import multivolumefile
        base = str(archive_path).rsplit(".001", 1)[0]
        ctx = multivolumefile.open(base, mode="rb")
    else:
        ctx = open(archive_path, "rb")

    with ctx as fh:
        with py7zr.SevenZipFile(fh, mode="r") as archive:
            total = archive.archiveinfo().uncompressed
            if send_output and total:
                cb = _make_extract_callback(total, send_output)
                archive.extractall(path=dest, callback=cb)
                send_output("\n")
            else:
                archive.extractall(path=dest)


def _extract_archive(
    archive_path: Path,
    dest: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Extract a tar.gz, .7z, or multi-volume .001 archive.

    For 7z archives: tries native 7z first (much faster), falls back to py7zr.
    """
    name = archive_path.name.lower()

    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive_path, "r:gz") as tar:
            _safe_tar_extractall(tar, dest, send_output)
    elif name.endswith(".tar"):
        with tarfile.open(archive_path, "r:") as tar:
            _safe_tar_extractall(tar, dest, send_output)
    elif name.endswith(".7z") or name.endswith(".001"):
        seven_zip = _find_7z()
        if seven_zip:
            if send_output:
                send_output("Using native 7z for extraction (fast path)...\n")
            _extract_7z_native(seven_zip, archive_path, dest, send_output)
        else:
            if send_output:
                send_output("Native 7z not found, using py7zr (slower)...\n")
            _extract_7z_py7zr(
                archive_path, dest, send_output,
                multivolume=name.endswith(".001"),
            )
    else:
        raise RuntimeError(f"Unsupported archive format: {archive_path.name}")


# ---------------------------------------------------------------------------
# Env creation — mirrors pythonEnv.ts createEnv
# ---------------------------------------------------------------------------

def create_env(
    install_path: str | Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Create a venv at ``ComfyUI/.venv`` via uv and copy site-packages.

    Mirrors pythonEnv.ts createEnv:
    1. uv venv --python {masterPython} {envPath}
    2. Copy site-packages from standalone-env to the new venv
    """
    install_path = Path(install_path)
    uv = get_uv_path(install_path)
    master_python = get_master_python_path(install_path)
    env_path = get_venv_dir(install_path)

    if not uv.exists():
        raise RuntimeError(f"uv not found at {uv}")
    if not master_python.exists():
        raise RuntimeError(f"Master Python not found at {master_python}")

    # Remove stale env from a previous failed attempt
    if env_path.exists():
        if send_output:
            send_output("Removing stale venv...\n")
        shutil.rmtree(env_path, ignore_errors=True)

    if send_output:
        send_output("Creating venv via uv...\n")

    # Set executable permission on Unix
    if sys.platform != "win32":
        _chmod_binaries(install_path / "standalone-env" / "bin")

    result = subprocess.run(
        [str(uv), "venv", "--python", str(master_python), str(env_path)],
        cwd=str(install_path),
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create venv: {result.stderr or result.stdout}"
        )

    # Copy site-packages from master to new env
    master_sp = find_site_packages(install_path / "standalone-env")
    env_sp = find_site_packages(env_path)

    if not master_sp or not env_sp:
        # Clean up on failure
        shutil.rmtree(env_path, ignore_errors=True)
        raise RuntimeError("Could not locate site-packages for venv.")

    if send_output:
        send_output("Copying site-packages from master env...\n")

    try:
        _copy_site_packages(master_sp, env_sp, send_output)
        # Codesign copied binaries on macOS
        from .macos import codesign_binaries
        codesign_binaries(env_sp, send_output)
    except Exception:
        shutil.rmtree(env_path, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Legacy layout migration
# ---------------------------------------------------------------------------

def migrate_env_layout(
    install_path: str | Path,
    send_output: Callable[[str], None] | None = None,
) -> bool:
    """Migrate from ``envs/default`` to ``ComfyUI/.venv``.

    Returns True if migration was performed, False if skipped.
    """
    install_path = Path(install_path)
    new_venv = get_venv_dir(install_path)
    legacy_env = install_path / _LEGACY_ENVS_DIR / _LEGACY_DEFAULT_ENV

    if new_venv.exists():
        return False
    if not legacy_env.exists():
        return False

    if send_output:
        send_output(f"Migrating venv: {legacy_env} -> {new_venv}\n")

    # Ensure parent exists (ComfyUI/ should already be there)
    new_venv.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_env), str(new_venv))

    # Fix pyvenv.cfg — update the home/prefix paths
    cfg_path = new_venv / "pyvenv.cfg"
    if cfg_path.exists():
        old_text = cfg_path.read_text(encoding="utf-8")
        new_text = old_text.replace(str(legacy_env), str(new_venv))
        if new_text != old_text:
            cfg_path.write_text(new_text, encoding="utf-8")

    # Fix shebangs on Unix
    if sys.platform != "win32":
        bin_dir = new_venv / "bin"
        if bin_dir.exists():
            old_prefix = str(legacy_env)
            new_prefix = str(new_venv)
            for entry in bin_dir.iterdir():
                if not entry.is_file():
                    continue
                try:
                    head = entry.read_bytes()[:256]
                    if b"#!" in head and old_prefix.encode() in head:
                        text = entry.read_text(encoding="utf-8")
                        entry.write_text(
                            text.replace(old_prefix, new_prefix),
                            encoding="utf-8",
                        )
                except (UnicodeDecodeError, OSError):
                    pass

    # Remove empty legacy dirs
    envs_dir = install_path / _LEGACY_ENVS_DIR
    try:
        if envs_dir.exists() and not any(envs_dir.iterdir()):
            envs_dir.rmdir()
    except OSError:
        pass

    # Codesign on macOS
    from .macos import codesign_binaries
    codesign_binaries(new_venv, send_output)

    if send_output:
        send_output("Migration complete.\n")
    return True


def _copy_site_packages(
    src: Path,
    dst: Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Copy site-packages contents from src to dst with progress."""
    # Count total items first
    items = list(src.iterdir())
    total = len(items)

    for i, item in enumerate(items, 1):
        dst_item = dst / item.name
        if item.is_dir():
            if dst_item.is_dir():
                shutil.rmtree(dst_item)
            elif dst_item.exists():
                dst_item.unlink()
            shutil.copytree(item, dst_item, dirs_exist_ok=True)
        else:
            if dst_item.is_dir():
                shutil.rmtree(dst_item)
            shutil.copy2(item, dst_item)

        if send_output and (i % 50 == 0 or i == total):
            pct = i * 100 // total
            send_output(f"\r  Copied {i}/{total} items ({pct}%)")

    if send_output:
        send_output("\n")


def _chmod_binaries(bin_dir: Path) -> None:
    """Set executable permission on binaries in a directory (Unix only)."""
    if not bin_dir.exists():
        return
    for entry in bin_dir.iterdir():
        if entry.is_file():
            entry.chmod(0o755)


# ---------------------------------------------------------------------------
# CUDA compatibility — detect driver, check torch, reinstall if needed
# ---------------------------------------------------------------------------

# (driver_major_min, cuda_version, cu_tag)
_DRIVER_CUDA_TABLE: list[tuple[int, str, str]] = [
    (580, "13.0", "cu130"),
    (570, "12.8", "cu128"),
    (560, "12.6", "cu126"),
    (550, "12.4", "cu124"),
    (525, "12.1", "cu121"),
]

# Reverse lookup: CUDA version string → minimum driver major
_CUDA_MIN_DRIVER: dict[str, int] = {cuda: drv for drv, cuda, _ in _DRIVER_CUDA_TABLE}


def _detect_nvidia_driver_version() -> str | None:
    """Query nvidia-smi for the driver version string (e.g. '590.48.01')."""
    result = _run_silent([
        "nvidia-smi",
        "--query-gpu=driver_version",
        "--format=csv,noheader",
    ])
    if result is None or result.returncode != 0:
        return None
    version = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    return version[0].strip() if version else None


def _driver_major(version: str) -> int | None:
    """Parse the major version from a driver string (e.g. '590.48.01' → 590)."""
    m = re.match(r"(\d+)", version)
    return int(m.group(1)) if m else None


def _best_cuda_for_driver(driver_major: int) -> tuple[str, str] | None:
    """Return (cuda_version, cu_tag) for the highest CUDA supported by *driver_major*."""
    for min_drv, cuda_ver, cu_tag in _DRIVER_CUDA_TABLE:
        if driver_major >= min_drv:
            return cuda_ver, cu_tag
    return None


def _torch_cuda_needs_driver(torch_cuda: str) -> int | None:
    """Return the minimum driver major required for *torch_cuda* (e.g. '13.0' → 580)."""
    return _CUDA_MIN_DRIVER.get(torch_cuda)


def ensure_cuda_compatible_torch(
    install_path: Path,
    send_output: Callable[[str], None] | None = None,
) -> bool:
    """Check if the venv's torch CUDA build matches the host NVIDIA driver.

    If the installed torch was built for a CUDA version that requires a newer
    driver than the host has, reinstall torch/torchvision/torchaudio from the
    appropriate PyTorch index URL.

    Returns True if torch was swapped, False if no swap was needed.
    """
    install_path = Path(install_path)
    venv = install_path / "ComfyUI" / ".venv"
    if sys.platform == "win32":
        venv_python = str(venv / "Scripts" / "python.exe")
    else:
        venv_python = str(venv / "bin" / "python3")

    if not Path(venv_python).exists():
        if send_output:
            send_output(f"Venv python not found at {venv_python}\n")
        return False

    # --- Detect host NVIDIA driver ---
    driver_str = _detect_nvidia_driver_version()
    if driver_str is None:
        if send_output:
            send_output("Could not detect NVIDIA driver version.\n")
        return False

    drv_major = _driver_major(driver_str)
    if drv_major is None:
        if send_output:
            send_output(f"Could not parse driver version: {driver_str}\n")
        return False

    best = _best_cuda_for_driver(drv_major)
    if best is None:
        if send_output:
            send_output(f"NVIDIA driver {driver_str} is too old (< 525). Cannot run CUDA torch.\n")
        return False

    best_cuda, best_tag = best
    if send_output:
        send_output(f"Detected NVIDIA driver: {driver_str} (max CUDA {best_cuda})\n")

    # --- Detect torch's CUDA version ---
    result = _run_silent(
        [venv_python, "-c", "import torch; print(torch.version.cuda)"],
        timeout=30,
    )
    if result is None or result.returncode != 0:
        if send_output:
            send_output("Could not detect torch CUDA version.\n")
        return False

    torch_cuda = result.stdout.decode("utf-8", errors="replace").strip()
    if not torch_cuda or torch_cuda == "None":
        if send_output:
            send_output("Installed torch is CPU-only, skipping CUDA check.\n")
        return False

    # --- Check compatibility ---
    needed_driver = _torch_cuda_needs_driver(torch_cuda)
    if needed_driver is not None and drv_major >= needed_driver:
        if send_output:
            send_output(f"Installed torch uses CUDA {torch_cuda} — compatible ✓\n")
        return False

    if send_output:
        send_output(
            f"Installed torch uses CUDA {torch_cuda} — incompatible with driver {driver_str}\n"
        )

    # --- Read manifest to pin torch versions ---
    manifest = read_manifest(install_path)
    packages = ["torch", "torchvision", "torchaudio"]
    if manifest:
        # Pin to the same base versions from the standalone env, just with a
        # different CUDA tag.  e.g. "2.10.0+cu130" → "2.10.0+cu128"
        for i, key in enumerate(["torch_version", "torchvision_version", "torchaudio_version"]):
            ver = manifest.get(key, "")
            if ver:
                base = ver.split("+")[0]  # strip +cu130
                packages[i] = f"{packages[i]}=={base}"

    # --- Reinstall with the best CUDA tag for this driver ---
    if send_output:
        send_output(f"Reinstalling PyTorch with CUDA {best_cuda} ({', '.join(packages)})...\n")

    index_url = f"https://download.pytorch.org/whl/{best_tag}"
    reinstall = subprocess.run(
        [
            venv_python, "-m", "pip", "install", "--force-reinstall",
            *packages,
            "--index-url", index_url,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if reinstall.returncode != 0:
        if send_output:
            send_output(f"Failed to reinstall torch: {reinstall.stderr or reinstall.stdout}\n")
        return False

    if send_output:
        send_output(f"Successfully reinstalled torch with CUDA {best_cuda} ✓\n")
    return True


# ---------------------------------------------------------------------------
# Master package cleanup — mirrors standalone.ts stripMasterPackages
# ---------------------------------------------------------------------------

BULKY_PREFIXES = ("torch", "nvidia", "triton", "cuda")


def strip_master_packages(
    install_path: str | Path,
    send_output: Callable[[str], None] | None = None,
) -> None:
    """Remove bulky packages (torch, nvidia, etc.) from master env's site-packages.

    Mirrors standalone.ts stripMasterPackages — after creating the default
    venv, these are no longer needed in the master env.
    Wrapped in try/except to match Desktop 2.0's console.warn behavior.
    """
    try:
        sp = find_site_packages(Path(install_path) / "standalone-env")
        if not sp or not sp.exists():
            return
        for entry in sp.iterdir():
            if entry.is_dir() and entry.name.lower().startswith(BULKY_PREFIXES):
                shutil.rmtree(entry, ignore_errors=True)
    except Exception as e:
        if send_output:
            send_output(f"⚠ Failed to strip master packages: {e}\n")


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------

def read_manifest(install_path: str | Path) -> dict[str, Any] | None:
    """Read manifest.json from an installation directory."""
    manifest_path = Path(install_path) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
