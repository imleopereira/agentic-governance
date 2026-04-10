"""Governance CLI — command-line interface for database migrations, audit
chain verification, event tailing, and budget inspection.

Entry point: ``governance`` (installed via pyproject.toml project.scripts).
"""
from __future__ import annotations

from .commands import main

__all__ = ["main"]
