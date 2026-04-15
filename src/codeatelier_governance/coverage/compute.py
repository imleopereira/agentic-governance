"""Coverage percentage computation for compliance reports.

Reads ``governance_wrapper_registrations`` joined against
``governance_policies`` to produce a single ``(coverage_pct, reason)``
tuple. ``reason`` is the DA-blocker discriminator that disambiguates
``coverage_pct=None`` between "no scope policies registered" and
"registry disabled".
"""
from __future__ import annotations

from typing import Any, Literal, cast

import structlog

logger = structlog.get_logger(__name__)

CoverageReason = Literal[
    "no_scope_policies_registered",
    "registry_disabled",
    "ok",
]


class CoverageComputer:
    """Compute ``coverage_pct`` for the compliance report.

    Collaborator injected into :class:`ReportGenerator`, mirroring the
    existing ``audit_module`` collaborator pattern.
    """

    def __init__(
        self,
        engine: Any = None,
        *,
        enabled: bool = True,
        active_window_days: int = 7,
    ) -> None:
        self._engine = engine
        self._enabled = enabled
        self._active_window_days = active_window_days

    async def compute(
        self, *, agent_id: str | None = None,
    ) -> tuple[float | None, CoverageReason | None]:
        """Return ``(coverage_pct, reason)``.

        * When the registry is disabled or the engine is unavailable,
          returns ``(None, "registry_disabled")``.
        * When no scope policies exist, returns
          ``(None, "no_scope_policies_registered")``.
        * Otherwise returns ``(active / total, "ok")``.
        """
        if not self._enabled or self._engine is None:
            return (None, "registry_disabled")

        try:
            from sqlalchemy import text

            async with self._engine.connect() as conn:
                total_res = await conn.execute(
                    text(
                        "SELECT COUNT(DISTINCT agent_id) FROM "
                        "governance_policies WHERE policy_type = 'scope'"
                    )
                )
                total_row = total_res.first()
                total = int(total_row[0]) if total_row and total_row[0] else 0

                if total == 0:
                    return (None, "no_scope_policies_registered")

                params: dict[str, Any] = {
                    "window": f"{self._active_window_days} days",
                }
                where_agent = ""
                if agent_id is not None:
                    where_agent = "AND agent_id = :agent_id"
                    params["agent_id"] = agent_id

                active_res = await conn.execute(
                    text(
                        "SELECT COUNT(DISTINCT agent_id) FROM "
                        "governance_wrapper_registrations "
                        "WHERE last_seen_at >= NOW() - CAST(:window AS INTERVAL) "
                        f"{where_agent}"
                    ),
                    params,
                )
                active_row = active_res.first()
                active = (
                    int(active_row[0]) if active_row and active_row[0] else 0
                )

            if agent_id is not None:
                # Per-agent: denominator is 1 if that agent has a scope
                # policy, 0 otherwise.
                pct = 1.0 if active > 0 else 0.0
                return (pct, "ok")

            pct = min(1.0, active / total) if total > 0 else 0.0
            return (pct, "ok")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "coverage.compute.failed",
                error_type=type(exc).__name__,
                detail="Coverage computation failed; returning registry_disabled.",
            )
            return (None, cast(CoverageReason, "registry_disabled"))
