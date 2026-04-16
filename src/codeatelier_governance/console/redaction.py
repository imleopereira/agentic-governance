"""Payload redaction for console response shapes (F3 v0.6).

This module used to own the implementation of :func:`redact_secrets`. As of
v0.6.1 the canonical implementation lives under
:mod:`codeatelier_governance.security.redaction` because the OTel audit
exporter (a sibling package, not a console dependant) also needs to call it
and ``audit/`` importing from ``console/`` would invert our layering.

This module remains importable as a back-compat alias until v0.7.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Back-compat aliases (removed v0.7)
# ---------------------------------------------------------------------------
# ``redact_secrets`` moved to ``codeatelier_governance.security.redaction`` in
# v0.6.1. External callers who imported
# ``from codeatelier_governance.console.redaction import redact_secrets``
# continue to work unchanged — this is an identity re-export, not a wrapper.
from ..security.redaction import redact_secrets  # Removed in v0.7

__all__ = ["redact_secrets"]
