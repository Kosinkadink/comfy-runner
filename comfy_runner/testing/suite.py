"""Test suite loading and discovery.

A test suite is a directory with the following structure::

    my-test-suite/
        suite.json              # metadata: name, description, required_models
        workflows/
            txt2img-sd15.json   # API-format workflow JSONs
            txt2img-sdxl.json
        baselines/
            txt2img-sd15/       # approved baseline outputs per workflow
                output_0.png
        config.json             # optional: comparison thresholds & overrides

``suite.json`` schema::

    {
        "name": "Basic Regression",
        "description": "Core txt2img/img2img smoke tests",
        "required_models": ["v1-5-pruned-emaonly.safetensors"],
        "models": [
            {
                "name": "v1-5-pruned-emaonly.safetensors",
                "directory": "checkpoints",
                "url": "https://huggingface.co/.../v1-5-pruned-emaonly.safetensors"
            }
        ],
        "max_runtime_s": 120
    }

The ``models`` array (optional) is a download manifest used by the test
runner to ensure required model files are present on the target before
running the suite. Each entry must have ``name``, ``directory``, and
``url``; ``token`` is optional (overrides the server's HF/ModelScope
token). Entries that already exist on disk are skipped.

The ``required_models`` array (legacy) is a flat list of filenames used
only for display.

``config.json`` schema::

    {
        "compare": {
            "image/*": {"method": "ssim", "threshold": 0.95},
            "video/*": {"method": "frame_ssim", "threshold": 0.90},
            "default": {"method": "existence"}
        },
        "overrides": {
            "seed": 42,
            "steps": null
        }
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Suite:
    """Loaded test suite ready for execution."""

    path: Path
    name: str
    description: str
    required_models: list[str]
    workflows: list[Path]
    baselines_dir: Path
    # Download manifest of model files (each entry: ``{name, directory,
    # url, [token]}``). Pre-flight downloaded onto the target by the test
    # runner before running workflows.
    models: list[dict[str, str]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    # Optional wall-clock budget for one target's run of this suite, in
    # seconds. If exceeded, the watchdog (in the server worker thread or
    # ``run_on_runpod``) aborts the run and dispatches ``on_overrun``.
    max_runtime_s: int | None = None

    def has_baseline(self, workflow_stem: str) -> bool:
        """Check if approved baselines exist for a workflow."""
        baseline_dir = self.baselines_dir / workflow_stem
        return baseline_dir.is_dir() and any(baseline_dir.iterdir())

    def get_baseline_files(self, workflow_stem: str) -> list[Path]:
        """Return sorted baseline files for a workflow."""
        baseline_dir = self.baselines_dir / workflow_stem
        if not baseline_dir.is_dir():
            return []
        return sorted(f for f in baseline_dir.iterdir() if f.is_file())

    def get_compare_config(self, mimetype: str = "") -> dict[str, Any]:
        """Return comparison config for a mimetype.

        Falls back to ``default`` if no specific rule matches.
        """
        compare = self.config.get("compare", {})
        if mimetype:
            # Try exact match first, then wildcard (e.g. "image/*")
            if mimetype in compare:
                return compare[mimetype]
            category = mimetype.split("/")[0] + "/*"
            if category in compare:
                return compare[category]
        return compare.get("default", {"method": "existence"})

    def get_overrides(self) -> dict[str, Any]:
        """Return workflow parameter overrides (e.g. fixed seed)."""
        return self.config.get("overrides", {})


def load_suite(suite_path: str | Path) -> TestSuite:
    """Load and validate a test suite from a directory.

    Raises ``ValueError`` if required files are missing or malformed.
    """
    suite_path = Path(suite_path).resolve()

    if not suite_path.is_dir():
        raise ValueError(f"Suite path is not a directory: {suite_path}")

    # Load suite.json
    suite_file = suite_path / "suite.json"
    if not suite_file.is_file():
        raise ValueError(f"Missing suite.json in {suite_path}")

    try:
        with open(suite_file) as f:
            meta = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Invalid suite.json: {exc}") from exc

    name = meta.get("name", suite_path.name)
    description = meta.get("description", "")
    required_models = meta.get("required_models", [])
    raw_max_runtime = meta.get("max_runtime_s")
    if raw_max_runtime is None:
        max_runtime_s: int | None = None
    else:
        try:
            max_runtime_s = int(raw_max_runtime)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid max_runtime_s in suite.json: {raw_max_runtime!r}"
            ) from exc
        if max_runtime_s <= 0:
            raise ValueError(
                f"max_runtime_s must be > 0, got {max_runtime_s}"
            )

    # Discover workflows
    workflows_dir = suite_path / "workflows"
    if not workflows_dir.is_dir():
        raise ValueError(f"Missing workflows/ directory in {suite_path}")

    workflows = sorted(workflows_dir.glob("*.json"))
    if not workflows:
        raise ValueError(f"No workflow JSON files in {workflows_dir}")

    # Optional download manifest (each entry must be a dict with at
    # least ``name``, ``directory``, and ``url`` string fields).
    raw_models = meta.get("models", [])
    if not isinstance(raw_models, list):
        raise ValueError(
            f"'models' in suite.json must be a list, got {type(raw_models).__name__}"
        )
    models: list[dict[str, str]] = []
    for i, entry in enumerate(raw_models):
        if not isinstance(entry, dict):
            raise ValueError(
                f"models[{i}] in suite.json must be an object, got {type(entry).__name__}"
            )
        for key in ("name", "directory", "url"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"models[{i}].{key} in suite.json is missing or not a non-empty string"
                )
        normalized = {
            "name": entry["name"],
            "directory": entry["directory"],
            "url": entry["url"],
        }
        token = entry.get("token")
        if token:
            if not isinstance(token, str):
                raise ValueError(
                    f"models[{i}].token in suite.json must be a string"
                )
            normalized["token"] = token
        models.append(normalized)

    # Baselines dir (may not exist yet)
    baselines_dir = suite_path / "baselines"

    # Load optional config.json
    config: dict[str, Any] = {}
    config_file = suite_path / "config.json"
    if config_file.is_file():
        try:
            with open(config_file) as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Invalid config.json: {exc}") from exc

    return Suite(
        path=suite_path,
        name=name,
        description=description,
        required_models=required_models,
        workflows=workflows,
        baselines_dir=baselines_dir,
        models=models,
        config=config,
        max_runtime_s=max_runtime_s,
    )


def discover_suites(search_dir: str | Path) -> list[Suite]:
    """Find and load all test suites under *search_dir*.

    A directory is considered a suite if it contains ``suite.json``.
    Skips invalid suites (logs a warning but does not raise).
    """
    search_dir = Path(search_dir).resolve()
    suites: list[TestSuite] = []

    if not search_dir.is_dir():
        return suites

    for suite_json in sorted(search_dir.rglob("suite.json")):
        try:
            suites.append(load_suite(suite_json.parent))
        except ValueError:
            continue

    return suites
