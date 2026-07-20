"""Pin the published package version against pyproject.toml.

Asserts the installed metadata version and the public
``codeatelier_governance.__version__`` accessor both match the version
declared in pyproject.toml, catching stale-wheel and mis-stamped builds
without a hardcoded literal that goes stale on every release bump.
"""
from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

import codeatelier_governance


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_pyproject_version_matches_expected() -> None:
    assert version("code-atelier-governance") == _pyproject_version()


def test_module_version_attribute_matches() -> None:
    # __version__ is sourced from importlib.metadata in __init__.py, so this
    # pins the public ``codeatelier_governance.__version__`` accessor too.
    assert codeatelier_governance.__version__ == _pyproject_version()
