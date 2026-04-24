"""Comparator registry and dispatch logic.

Each comparator is a callable with the signature::

    def compare(baseline: Path, test: Path, **kwargs) -> CompareResult

Comparators are registered by name in ``REGISTRY``.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass
class CompareResult:
    """Result of comparing a test output against a baseline."""

    method: str
    score: float | None = None  # 0.0–1.0 for similarity, None for existence
    passed: bool = True
    threshold: float | None = None
    diff_artifact: Path | None = None  # e.g. SSIM diff heatmap
    details: dict[str, Any] = field(default_factory=dict)


class Comparator(Protocol):
    def __call__(
        self, baseline: Path, test: Path, **kwargs: Any,
    ) -> CompareResult: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Comparator] = {}


def register(name: str, fn: Comparator) -> None:
    """Register a comparator function under *name*."""
    REGISTRY[name] = fn


def get_comparator(name: str) -> Comparator:
    """Look up a comparator by name.

    Raises ``KeyError`` if not found.
    """
    if name not in REGISTRY:
        raise KeyError(
            f"Unknown comparator '{name}'. "
            f"Available: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[name]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _guess_mimetype(path: Path) -> str:
    """Guess the MIME type of a file, defaulting to ''."""
    mt, _ = mimetypes.guess_type(str(path))
    return mt or ""


def compare_outputs(
    baseline: Path,
    test: Path,
    compare_config: dict[str, Any] | None = None,
) -> CompareResult:
    """Compare a test output against a baseline using config rules.

    *compare_config* should be the result of ``Suite.get_compare_config()``
    for the relevant mimetype, e.g.::

        {"method": "ssim", "threshold": 0.95}

    If not provided, falls back to ``existence``.
    """
    if compare_config is None:
        compare_config = {"method": "existence"}

    method = compare_config.get("method", "existence")
    threshold = compare_config.get("threshold")
    kwargs: dict[str, Any] = {}
    if threshold is not None:
        kwargs["threshold"] = threshold

    # Pass through any extra config keys as kwargs
    for k, v in compare_config.items():
        if k not in ("method", "threshold"):
            kwargs[k] = v

    comparator = get_comparator(method)
    return comparator(baseline, test, **kwargs)


# ---------------------------------------------------------------------------
# Auto-register built-in comparators on import
# ---------------------------------------------------------------------------

from . import comparators as _comparators  # noqa: E402, F401
