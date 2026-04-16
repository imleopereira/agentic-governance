"""Cross-cutting security primitives.

This package holds security utilities that are shared across multiple
top-level packages (``audit``, ``console``, ``gates``, ...) and therefore
cannot live inside any one of them without creating a layering violation.

Currently houses:

* :func:`redact_secrets` — stdlib-only, stateless secret-pattern masking
  used by both the console response pipeline (F3) and the audit OTel
  exporter (v0.6.1 S5 fix).
"""
from __future__ import annotations

from .redaction import redact_secrets

__all__ = ["redact_secrets"]
