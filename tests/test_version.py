"""BLOCKER 0: pin the published package version.

The release is gated on pyproject.toml matching ``EXPECTED_VERSION``.
This test asserts the installed metadata matches and is the literal
tag blocker check the team review surfaced.
"""
from __future__ import annotations

from importlib.metadata import version

import codeatelier_governance


EXPECTED_VERSION = "0.7.0"


def test_pyproject_version_matches_expected() -> None:
    assert version("code-atelier-governance") == EXPECTED_VERSION


def test_module_version_attribute_matches() -> None:
    # __version__ is sourced from importlib.metadata in __init__.py, so
    # this is the same fact stated twice — but it pins the public
    # ``codeatelier_governance.__version__`` accessor users rely on.
    assert codeatelier_governance.__version__ == EXPECTED_VERSION
