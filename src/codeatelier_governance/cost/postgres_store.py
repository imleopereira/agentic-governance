"""Postgres-backed cost store using SQLAlchemy 2.0 + asyncpg.

Multi-process correct via row-level atomic UPSERT:

    INSERT INTO governance_cost_session_usage (...)
    VALUES (...)
    ON CONFLICT (agent_id, session_id) DO UPDATE SET
        usd_used    = governance_cost_session_usage.usd_used    + EXCLUDED.usd_used,
        tokens_used = governance_cost_session_usage.tokens_used + EXCLUDED.tokens_used,
        last_updated = NOW()

Postgres handles concurrent writers to the same row by serializing the
UPDATE phase — no SELECT FOR UPDATE needed, no advisory locks. Two workers
calling ``track`` for the same (agent, session) simultaneously land both
deltas with no lost updates.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..utils import normalize_db_url
from .errors import CostError
from .store import CostStore


class PostgresCostStore(CostStore):
    """SQLAlchemy/asyncpg-backed cost store."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: AsyncEngine | None = None,
    ) -> None:
        if engine is not None:
            self._engine: AsyncEngine = engine
            self._owns_engine = False
        elif database_url is not None:
            self._engine = create_async_engine(
                normalize_db_url(database_url, component="cost store"),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
            )
            self._owns_engine = True
        else:
            raise ValueError(
                "PostgresCostStore requires either database_url or engine"
            )

    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int,
        usd: float,
        model: str | None = None,
    ) -> None:
        # Day boundary is computed by Postgres atomically with the INSERT
        # so a track() call straddling UTC midnight cannot drop the row
        # into yesterday's bucket while the next check_or_raise reads from
        # tomorrow's. Single source of truth: the database clock.
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO governance_cost_session_usage
                            (agent_id, session_id, usd_used, tokens_used, last_updated, started_at)
                        VALUES (:agent_id, :session_id, :usd, :tokens, NOW(), NOW())
                        ON CONFLICT (agent_id, session_id) DO UPDATE SET
                            usd_used    = governance_cost_session_usage.usd_used    + EXCLUDED.usd_used,
                            tokens_used = governance_cost_session_usage.tokens_used + EXCLUDED.tokens_used,
                            last_updated = NOW()
                        """
                    ),
                    {
                        "agent_id": agent_id,
                        "session_id": str(session_id),
                        "usd": usd,
                        "tokens": tokens,
                    },
                )
                await conn.execute(
                    text(
                        """
                        INSERT INTO governance_cost_agent_daily
                            (agent_id, day_utc, usd_used, tokens_used, last_updated)
                        VALUES (:agent_id, (NOW() AT TIME ZONE 'UTC')::DATE, :usd, :tokens, NOW())
                        ON CONFLICT (agent_id, day_utc) DO UPDATE SET
                            usd_used    = governance_cost_agent_daily.usd_used    + EXCLUDED.usd_used,
                            tokens_used = governance_cost_agent_daily.tokens_used + EXCLUDED.tokens_used,
                            last_updated = NOW()
                        """
                    ),
                    {
                        "agent_id": agent_id,
                        "usd": usd,
                        "tokens": tokens,
                    },
                )
                # Per-model daily breakdown (v0.3)
                if model is not None:
                    await conn.execute(
                        text(
                            """
                            INSERT INTO governance_cost_model_daily
                                (agent_id, model, day_utc, usd_used, tokens_used, last_updated)
                            VALUES (:agent_id, :model, (NOW() AT TIME ZONE 'UTC')::DATE,
                                    :usd, :tokens, NOW())
                            ON CONFLICT (agent_id, model, day_utc) DO UPDATE SET
                                usd_used    = governance_cost_model_daily.usd_used    + EXCLUDED.usd_used,
                                tokens_used = governance_cost_model_daily.tokens_used + EXCLUDED.tokens_used,
                                last_updated = NOW()
                            """
                        ),
                        {
                            "agent_id": agent_id,
                            "model": model,
                            "usd": usd,
                            "tokens": tokens,
                        },
                    )
        except Exception as exc:
            raise CostError(
                f"postgres cost.track failed: {type(exc).__name__}"
            ) from exc

    async def get_session_usage(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> tuple[float, int]:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT usd_used, tokens_used FROM governance_cost_session_usage "
                    "WHERE agent_id = :agent_id AND session_id = :sid"
                ),
                {"agent_id": agent_id, "sid": str(session_id)},
            )
            row = res.first()
        if row is None:
            return (0.0, 0)
        return (float(row[0]), int(row[1]))

    async def get_agent_daily_usage(
        self,
        agent_id: str,
    ) -> tuple[float, int]:
        # Same source of truth as track(): Postgres CURRENT_DATE in UTC.
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT usd_used, tokens_used FROM governance_cost_agent_daily "
                    "WHERE agent_id = :agent_id "
                    "  AND day_utc = (NOW() AT TIME ZONE 'UTC')::DATE"
                ),
                {"agent_id": agent_id},
            )
            row = res.first()
        if row is None:
            return (0.0, 0)
        return (float(row[0]), int(row[1]))

    async def get_session_start_time(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> datetime | None:
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT started_at FROM governance_cost_session_usage "
                    "WHERE agent_id = :agent_id AND session_id = :sid"
                ),
                {"agent_id": agent_id, "sid": str(session_id)},
            )
            row = res.first()
        if row is None or row[0] is None:
            return None
        val = row[0]
        if isinstance(val, datetime):
            return val
        return None

    async def get_model_breakdown(
        self, agent_id: str
    ) -> dict[str, dict[str, float]]:
        """Return per-model usage for today: {model: {usd: X, tokens: Y}}."""
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT model, usd_used, tokens_used "
                    "FROM governance_cost_model_daily "
                    "WHERE agent_id = :agent_id "
                    "  AND day_utc = (NOW() AT TIME ZONE 'UTC')::DATE"
                ),
                {"agent_id": agent_id},
            )
            rows = list(res.mappings())
        return {
            row["model"]: {
                "usd": float(row["usd_used"]),
                "tokens": float(int(row["tokens_used"])),
            }
            for row in rows
        }

    async def get_session_and_daily_usage(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> tuple[float, int, float, int]:
        """Return session and daily usage in a single DB round-trip.

        Returns ``(session_usd, session_tokens, daily_usd, daily_tokens)``.
        This combines ``get_session_usage`` and ``get_agent_daily_usage``
        into one query to halve the pre-call enforcement latency.

        NOTE: this is a READ used by the pre-call check, with no reservation.
        The atomic UPSERT in track() prevents lost updates (the recorded total
        is always correct), but it does NOT prevent OVER-ADMISSION: N concurrent
        callers can all read the same pre-track balance, all pass the cap check,
        then all track — overshooting the cap by up to (N-1) x per-call cost.
        The cap is therefore a soft ceiling under concurrency, not a hard limit;
        closing that window would require a reserve-then-settle counter (an
        atomic increment-and-check inside the enforcement path). FOR UPDATE is
        not used here because Postgres does not support it on the nullable side
        of an outer join.
        """
        async with self._engine.begin() as conn:
            res = await conn.execute(
                text(
                    """
                    SELECT
                        COALESCE(s.usd_used, 0) AS s_usd,
                        COALESCE(s.tokens_used, 0) AS s_tok,
                        COALESCE(d.usd_used, 0) AS d_usd,
                        COALESCE(d.tokens_used, 0) AS d_tok
                    FROM (SELECT 1) AS _dummy
                    LEFT JOIN governance_cost_session_usage s
                        ON s.agent_id = :agent_id AND s.session_id = :sid
                    LEFT JOIN governance_cost_agent_daily d
                        ON d.agent_id = :agent_id
                        AND d.day_utc = (NOW() AT TIME ZONE 'UTC')::DATE
                    """
                ),
                {"agent_id": agent_id, "sid": str(session_id)},
            )
            row = res.first()
        if row is None:
            return (0.0, 0, 0.0, 0)
        return (float(row[0]), int(row[1]), float(row[2]), int(row[3]))

    async def get_session_elapsed_seconds(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> float | None:
        """Return elapsed seconds since session start, computed in Postgres.

        Uses Postgres NOW() for both timestamps to avoid mixed-clock skew
        between Python and database servers.
        """
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM NOW() - started_at) "
                    "FROM governance_cost_session_usage "
                    "WHERE agent_id = :agent_id AND session_id = :sid"
                ),
                {"agent_id": agent_id, "sid": str(session_id)},
            )
            row = res.first()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    async def close(self) -> None:
        """Dispose the engine only if this store owns it."""
        if self._owns_engine:
            await self._engine.dispose()
