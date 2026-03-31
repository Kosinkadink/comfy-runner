"""Collect hardware/system information matching Desktop 2.0's SystemInfo shape.

All data is gathered from stdlib + subprocess only — no extra pip dependencies.
Every subprocess call uses a timeout and catches all exceptions so that missing
tools or unexpected output never raise; missing data is represented as None.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import get_installations_dir, list_installations
from .environment import _run_silent, detect_gpu

# Paths that can be monkeypatched in tests
_PROC_CPUINFO = "/proc/cpuinfo"
_PROC_MEMINFO = "/proc/meminfo"

# ---------------------------------------------------------------------------
# GPU label mapping
# ---------------------------------------------------------------------------

_GPU_LABEL: dict[str, str] = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "mps": "Apple Silicon",
}


# ---------------------------------------------------------------------------
# GPU detail collection
# ---------------------------------------------------------------------------

def _get_nvidia_gpus() -> list[dict[str, Any]]:
    """Query nvidia-smi for per-GPU name, VRAM, and driver version."""
    result = _run_silent([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ])
    if result is None or result.returncode != 0:
        return []
    gpus: list[dict[str, Any]] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            vram = int(float(parts[1]))
        except (ValueError, IndexError):
            vram = None
        gpus.append({
            "vendor": "NVIDIA",
            "model": parts[0],
            "vram_mb": vram,
            "driver_version": parts[2],
        })
    return gpus


def _get_nvidia_driver_version() -> str | None:
    """Return the NVIDIA driver version string, or None."""
    result = _run_silent([
        "nvidia-smi",
        "--query-gpu=driver_version",
        "--format=csv,noheader",
    ])
    if result is not None and result.returncode == 0:
        ver = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        if ver and ver[0].strip():
            return ver[0].strip()

    # Fallback: parse plain nvidia-smi banner
    result = _run_silent(["nvidia-smi"])
    if result is not None and result.returncode == 0:
        stdout = result.stdout.decode("utf-8", errors="replace")
        m = re.search(r"driver version\s*:\s*([\d.]+)", stdout, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _is_nvidia_driver_supported(version: str | None) -> bool | None:
    """Check if the NVIDIA driver version is >= 580. Returns None if unknown."""
    if version is None:
        return None
    m = re.match(r"(\d+)", version)
    if not m:
        return None
    return int(m.group(1)) >= 580


def _get_linux_gpus_lspci() -> list[dict[str, Any]]:
    """Parse ``lspci -vmm`` for VGA / 3D / Display controller stanzas."""
    result = _run_silent(["lspci", "-vmm"])
    if result is None or result.returncode != 0:
        return []
    stdout = result.stdout.decode("utf-8", errors="replace")
    gpus: list[dict[str, Any]] = []
    stanza: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            # End of stanza
            cls = stanza.get("Class", "")
            if any(kw in cls for kw in ("VGA", "3D", "Display")):
                vendor_str = stanza.get("Vendor", stanza.get("SVendor", ""))
                model_str = stanza.get("Device", "Unknown")
                vendor = _map_vendor(vendor_str)
                gpus.append({
                    "vendor": vendor,
                    "model": model_str,
                    "vram_mb": None,
                    "driver_version": None,
                })
            stanza = {}
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            stanza[key.strip()] = val.strip()
    # Handle last stanza if file doesn't end with blank line
    if stanza:
        cls = stanza.get("Class", "")
        if any(kw in cls for kw in ("VGA", "3D", "Display")):
            vendor_str = stanza.get("Vendor", stanza.get("SVendor", ""))
            model_str = stanza.get("Device", "Unknown")
            vendor = _map_vendor(vendor_str)
            gpus.append({
                "vendor": vendor,
                "model": model_str,
                "vram_mb": None,
                "driver_version": None,
            })
    return gpus


def _map_vendor(vendor_str: str) -> str:
    """Map a vendor string to a canonical vendor name."""
    low = vendor_str.lower()
    if "nvidia" in low:
        return "NVIDIA"
    if "advanced micro" in low or "amd" in low:
        return "AMD"
    if "intel" in low:
        return "Intel"
    return vendor_str


def _get_gpus() -> list[dict[str, Any]]:
    """Collect GPU info from nvidia-smi and (on Linux) lspci, deduplicated.

    nvidia-smi provides richer data (VRAM, driver) so its entries take priority.
    lspci entries are only added for GPUs not already covered by nvidia-smi
    (e.g. AMD or Intel GPUs in a mixed system).
    """
    gpus = _get_nvidia_gpus()
    nvidia_vendors = {g.get("vendor", "").upper() for g in gpus}

    if sys.platform == "linux":
        for g in _get_linux_gpus_lspci():
            # Skip lspci entries whose vendor is already covered by nvidia-smi
            if g.get("vendor", "").upper() in nvidia_vendors:
                continue
            gpus.append(g)

    return gpus


# ---------------------------------------------------------------------------
# CPU info
# ---------------------------------------------------------------------------

def _get_cpu_info() -> dict[str, Any]:
    """Gather CPU model, core counts, speed, and manufacturer."""
    model: str = "Unknown"
    cores: int = os.cpu_count() or 1
    physical_cores: int | None = None
    speed_ghz: float | None = None

    if sys.platform == "linux":
        model, physical_cores, speed_ghz = _cpu_info_linux()
    elif sys.platform == "darwin":
        model, physical_cores, speed_ghz = _cpu_info_darwin()
    else:
        # Windows / fallback
        model = platform.processor() or "Unknown"

    manufacturer: str | None = None
    model_low = model.lower()
    if "intel" in model_low:
        manufacturer = "Intel"
    elif "amd" in model_low:
        manufacturer = "AMD"
    elif "apple" in model_low:
        manufacturer = "Apple"

    return {
        "model": model,
        "cores": cores,
        "physical_cores": physical_cores,
        "speed_ghz": speed_ghz,
        "manufacturer": manufacturer,
    }


def _cpu_info_linux() -> tuple[str, int | None, float | None]:
    model = "Unknown"
    physical_cores: int | None = None
    speed_ghz: float | None = None
    try:
        with open(_PROC_CPUINFO, "r") as f:
            content = f.read()
        # Model name
        m = re.search(r"^model name\s*:\s*(.+)$", content, re.MULTILINE)
        if m:
            model = m.group(1).strip()
        # Physical cores: count unique core ids per physical id
        phys_cores: set[tuple[str, str]] = set()
        cur_phys_id = "0"
        for line in content.splitlines():
            if line.startswith("physical id"):
                cur_phys_id = line.split(":", 1)[1].strip()
            elif line.startswith("core id"):
                core_id = line.split(":", 1)[1].strip()
                phys_cores.add((cur_phys_id, core_id))
        if phys_cores:
            physical_cores = len(phys_cores)
        # Speed
        m = re.search(r"^cpu MHz\s*:\s*([\d.]+)", content, re.MULTILINE)
        if m:
            speed_ghz = round(float(m.group(1)) / 1000.0, 2)
    except (OSError, ValueError):
        pass
    return model, physical_cores, speed_ghz


def _cpu_info_darwin() -> tuple[str, int | None, float | None]:
    model = "Unknown"
    physical_cores: int | None = None
    speed_ghz: float | None = None

    r = _run_silent(["sysctl", "-n", "machdep.cpu.brand_string"])
    if r and r.returncode == 0:
        model = r.stdout.decode("utf-8", errors="replace").strip() or model

    r = _run_silent(["sysctl", "-n", "hw.physicalcpu"])
    if r and r.returncode == 0:
        try:
            physical_cores = int(r.stdout.decode("utf-8", errors="replace").strip())
        except ValueError:
            pass

    r = _run_silent(["sysctl", "-n", "hw.cpufrequency_max"])
    if r and r.returncode == 0:
        try:
            speed_ghz = round(int(r.stdout.decode("utf-8", errors="replace").strip()) / 1e9, 2)
        except ValueError:
            pass

    return model, physical_cores, speed_ghz


# ---------------------------------------------------------------------------
# OS info
# ---------------------------------------------------------------------------

def _get_os_info() -> dict[str, Any]:
    """Gather platform, architecture, and OS version/distro details."""
    info: dict[str, Any] = {
        "platform": sys.platform,
        "arch": platform.machine(),
        "os_version": platform.release(),
        "os_distro": None,
        "os_release": None,
    }

    if sys.platform == "linux":
        try:
            with open("/etc/os-release", "r") as f:
                content = f.read()
            for line in content.splitlines():
                if line.startswith("PRETTY_NAME="):
                    info["os_distro"] = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("VERSION_ID="):
                    info["os_release"] = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    elif sys.platform == "darwin":
        mac_ver = platform.mac_ver()[0]
        if mac_ver:
            info["os_release"] = mac_ver
            info["os_distro"] = f"macOS {mac_ver}"
    elif sys.platform == "win32":
        info["os_release"] = platform.version()
        edition = ""
        if hasattr(platform, "win32_edition"):
            edition = platform.win32_edition() or ""
        info["os_distro"] = f"Windows {edition}".strip()

    return info


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def _get_total_memory_gb() -> int:
    """Return total physical RAM in GB (rounded)."""
    # Linux — /proc/meminfo
    if sys.platform == "linux":
        try:
            with open(_PROC_MEMINFO, "r") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 * 1024))
        except (OSError, ValueError, IndexError):
            pass

    # macOS — sysctl
    if sys.platform == "darwin":
        r = _run_silent(["sysctl", "-n", "hw.memsize"])
        if r and r.returncode == 0:
            try:
                return round(int(r.stdout.decode("utf-8", errors="replace").strip()) / (1024 ** 3))
            except ValueError:
                pass

    # Windows — ctypes
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.wintypes.DWORD),
                    ("dwMemoryLoad", ctypes.wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                return round(mem.ullTotalPhys / (1024 ** 3))
        except Exception:
            pass

    # Fallback — os.sysconf (Linux / macOS)
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return round((page_size * page_count) / (1024 ** 3))
    except (AttributeError, ValueError, OSError):
        pass

    return 0


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def _get_disk_info(path: str | Path | None = None) -> dict[str, float]:
    """Return free and total disk space in GB for the given path."""
    try:
        target = str(path) if path else str(get_installations_dir())
        usage = shutil.disk_usage(target)
        return {
            "free_gb": round(usage.free / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1),
        }
    except (OSError, ValueError):
        return {"free_gb": 0.0, "total_gb": 0.0}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_system_info() -> dict[str, Any]:
    """Collect full system info matching Desktop 2.0's SystemInfo shape.

    Returns a dict with GPU, CPU, OS, memory, disk, and installation details.
    Never raises — all missing data is represented as None or safe defaults.
    """
    # GPU vendor detection
    gpu_vendor_raw = detect_gpu()
    gpu_vendor: str | None = gpu_vendor_raw if gpu_vendor_raw != "cpu" else None
    gpu_label: str | None = _GPU_LABEL.get(gpu_vendor_raw) if gpu_vendor else None

    gpus = _get_gpus()

    nvidia_driver = _get_nvidia_driver_version()
    nvidia_supported = _is_nvidia_driver_supported(nvidia_driver)

    os_info = _get_os_info()
    cpu = _get_cpu_info()
    mem = _get_total_memory_gb()
    disk = _get_disk_info()

    # Installation inventory
    try:
        raw_installs = list_installations()
    except Exception:
        raw_installs = {}

    installations: list[dict[str, str]] = []
    for name, rec in raw_installs.items():
        installations.append({
            "name": name,
            "variant": rec.get("variant", ""),
            "status": rec.get("status", ""),
            "release_tag": rec.get("release_tag", ""),
        })

    return {
        "gpu_vendor": gpu_vendor,
        "gpu_label": gpu_label,
        "gpus": gpus,
        "nvidia_driver_version": nvidia_driver,
        "nvidia_driver_supported": nvidia_supported,
        "platform": os_info["platform"],
        "arch": os_info["arch"],
        "os_version": os_info["os_version"],
        "os_distro": os_info["os_distro"],
        "os_release": os_info["os_release"],
        "total_memory_gb": mem,
        "cpu_model": cpu["model"],
        "cpu_cores": cpu["cores"],
        "cpu_physical_cores": cpu["physical_cores"],
        "cpu_speed_ghz": cpu["speed_ghz"],
        "cpu_manufacturer": cpu["manufacturer"],
        "disk_free_gb": disk["free_gb"],
        "disk_total_gb": disk["total_gb"],
        "installation_count": len(installations),
        "installations": installations,
    }
