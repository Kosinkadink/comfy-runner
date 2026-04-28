"""Pluggable output comparison system.

Comparators are registered by name and dispatched based on mimetype
rules from the suite's ``config.json``.

Heavy dependencies (Pillow, numpy, opencv) are lazy-imported so the
base package stays lightweight.  The ``existence`` comparator requires
zero external deps and is always available as the default fallback.
"""

from __future__ import annotations

from .registry import REGISTRY, CompareResult, compare_outputs, get_comparator

__all__ = ["REGISTRY", "CompareResult", "compare_outputs", "get_comparator"]
