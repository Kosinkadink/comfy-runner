"""Global config + installation registry stored at ~/.comfy-runner/config.json.

Set COMFY_RUNNER_HOME to override the default config directory.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("COMFY_RUNNER_HOME", Path.home() / ".comfy-runner"))
CONFIG_FILE = CONFIG_DIR / "config.json"

# Default shared directory matches ComfyUI Desktop 2.0 (~/ComfyUI-Shared)
DEFAULT_SHARED_DIR = str(Path.home() / "ComfyUI-Shared")

DEFAULT_CONFIG: dict[str, Any] = {
    "installations_dir": str(CONFIG_DIR / "installations"),
    "installations": {},
    "tunnel": {},
    "shared_dir": DEFAULT_SHARED_DIR,
}


def load_config() -> dict[str, Any]:
    """Load config from disk, creating defaults if missing."""
    from safe_file import atomic_read
    raw = atomic_read(CONFIG_FILE)
    if not raw:
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key, default in DEFAULT_CONFIG.items():
                data.setdefault(key, copy.deepcopy(default))
            return data
    except json.JSONDecodeError:
        pass
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Persist config to disk."""
    from safe_file import atomic_write
    atomic_write(CONFIG_FILE, json.dumps(config, indent=2) + "\n", backup=True)


def get_installation(name: str) -> dict[str, Any] | None:
    """Get a single installation record by name."""
    config = load_config()
    return config["installations"].get(name)


def set_installation(name: str, record: dict[str, Any]) -> None:
    """Create or update an installation record."""
    config = load_config()
    config["installations"][name] = record
    save_config(config)


def remove_installation(name: str) -> bool:
    """Remove an installation record. Returns True if it existed."""
    config = load_config()
    if name in config["installations"]:
        del config["installations"][name]
        save_config(config)
        return True
    return False


def list_installations() -> dict[str, dict[str, Any]]:
    """Return all installation records."""
    config = load_config()
    return config["installations"]


def get_installations_dir() -> Path:
    """Return the base directory for installations."""
    config = load_config()
    return Path(config["installations_dir"]).expanduser()


def get_tunnel_config(provider: str) -> dict[str, Any]:
    """Return tunnel configuration for a provider (e.g. 'ngrok')."""
    config = load_config()
    return config.get("tunnel", {}).get(provider, {})


def set_tunnel_config(provider: str, data: dict[str, Any]) -> None:
    """Set tunnel configuration for a provider."""
    config = load_config()
    tunnel = config.setdefault("tunnel", {})
    tunnel[provider] = data
    save_config(config)


def get_shared_dir() -> str:
    """Return the configured shared directory path.

    Defaults to ``~/ComfyUI-Shared`` to match ComfyUI Desktop 2.0.
    """
    config = load_config()
    return config.get("shared_dir", DEFAULT_SHARED_DIR)


def set_shared_dir(path: str) -> None:
    """Set the shared directory path."""
    config = load_config()
    config["shared_dir"] = path
    save_config(config)


def get_github_token() -> str:
    """Get GitHub token from config or environment."""
    import os

    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    config = load_config()
    return config.get("github_token", "")
