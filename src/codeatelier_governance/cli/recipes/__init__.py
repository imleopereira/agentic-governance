"""Recipe templates for ``governance init agent-<kind>``.

Each recipe is a Python source file with Jinja2-free ``{placeholder}``
substitution tokens resolved via ``str.format(**context)`` at generation
time. Templates are shipped as ``.py.tmpl`` package data so they are not
accidentally type-checked or executed as real modules at import time.
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path

RECIPE_KINDS = ("customer-support", "data-enrichment", "internal-research")


def template_path(kind: str) -> Path:
    """Resolve the on-disk path for a recipe template.

    Raises FileNotFoundError for an unknown kind — the caller is expected
    to validate against :data:`RECIPE_KINDS` first and emit a user-facing
    error with the list of supported kinds.
    """
    if kind not in RECIPE_KINDS:
        raise FileNotFoundError(f"unknown recipe kind: {kind!r}")
    slug = kind.replace("-", "_")
    res = resources.files(__name__) / f"{slug}.py.tmpl"
    return Path(str(res))


__all__ = ["RECIPE_KINDS", "template_path"]
