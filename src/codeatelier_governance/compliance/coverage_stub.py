"""Default no-op coverage collaborator for :class:`ReportGenerator`.

When ``ReportGenerator`` is constructed without an explicit ``coverage``
argument (e.g. in tests, or from legacy call sites), this stub is used
so the generator can still produce a valid :class:`ComplianceReport`
with an explicit ``coverage_pct_reason="registry_disabled"``.
"""
from __future__ import annotations

from typing import Literal


class _DisabledCoverageComputer:
    """Always returns ``(None, "registry_disabled")``.

    This is the correct default for a generator constructed without the
    F9 coverage collaborator. It preserves backwards compatibility for
    v0.5.x call sites while ensuring the new ``coverage_pct_reason``
    field is always populated — never the ambiguous ``None`` that the
    DA flagged as a P0 blocker.
    """

    async def compute(
        self, *, agent_id: str | None = None,
    ) -> tuple[
        float | None,
        Literal["no_scope_policies_registered", "registry_disabled", "ok"] | None,
    ]:
        return (None, "registry_disabled")
