from __future__ import annotations

import sys
from pathlib import Path

import pytest

COMFY_RUNNER_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def tmp_config_dir(tmp_path, monkeypatch):
    """Redirect all config / cache paths into tmp_path so tests never touch the real home."""
    config_dir = tmp_path / ".comfy-runner"
    config_dir.mkdir()

    # Ensure the comfy-runner root is importable (for `safe_file`)
    root_str = str(COMFY_RUNNER_ROOT)
    if root_str not in sys.path:
        monkeypatch.syspath_prepend(root_str)

    import comfy_runner.config as cfg_mod
    import comfy_runner.cache as cache_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.json")

    cache_dir = config_dir / "cache"
    monkeypatch.setattr(cache_mod, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache_mod, "CACHE_META_FILE", cache_dir / "cache-meta.json")

    return tmp_path


@pytest.fixture()
def fake_install(tmp_path, tmp_config_dir):
    """Create a minimal fake ComfyUI installation and register it in config."""
    install_dir = tmp_path / "install"
    (install_dir / "ComfyUI" / "custom_nodes").mkdir(parents=True)

    from comfy_runner.config import set_installation

    set_installation("main", {
        "status": "installed",
        "path": str(install_dir),
    })

    return install_dir
