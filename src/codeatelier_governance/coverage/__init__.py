"""F9 Wrapper Coverage Registry.

Records every SDK process that has wrapped an LLM client (via
``wrap_openai`` / ``wrap_anthropic``) so that :class:`ComplianceReport`
can quantify wrapper coverage as a fraction of declared scope policies.

Storage is hybrid:

* Primary: an in-memory dict on the :class:`WrapperRegistry` instance,
  local to the current SDK process.
* Mirror: a Postgres table ``governance_wrapper_registrations`` flushed
  on :meth:`GovernanceSDK.start` and on every audit flush cycle. The
  mirror lets :class:`ReportGenerator` running in another process see
  wrappers registered by peer replicas.

Every Postgres write is fire-and-forget: failures are logged at WARN
and NEVER raised into host code — invariant #1 (host app continues
working if governance DB is unreachable).

See ``decisions/2026-04-15-f9-wrapper-coverage-design.md`` for the full
design rationale, including the append-only carve-out and the PII
hostname-hashing decision.
"""
from __future__ import annotations

from .compute import CoverageComputer, CoverageReason
from .registry import WrapperRegistration, WrapperRegistry

__all__ = [
    "CoverageComputer",
    "CoverageReason",
    "WrapperRegistration",
    "WrapperRegistry",
]
