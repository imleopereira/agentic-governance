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

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .errors import CostError
from .store import CostStore, _utc_day_start


def _normalize_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    raise ValueError(
        "cost store: expected a postgresql:// connection string."
    )


class PostgresCostStore(CostStore):
    """SQLAlchemy/asyncpg-backed cost store."""

    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(
            _normalize_url(database_url),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )

    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int,
        usd: float,
    ) -> None:
        now = datetime.now(timezone.utc)
        day = _utc_day_start(now).date()
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO governance_cost_session_usage
                            (agent_id, session_id, usd_used, tokens_used, last_updated)
                        VALUES (:agent_id, :session_id, :usd, :tokens, NOW())
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
                        VALUES (:agent_id, :day, :usd, :tokens, NOW())
                        ON CONFLICT (agent_id, day_utc) DO UPDATE SET
                            usd_used    = governance_cost_agent_daily.usd_used    + EXCLUDED.usd_used,
                            tokens_used = governance_cost_agent_daily.tokens_used + EXCLUDED.tokens_used,
                            last_updated = NOW()
                        """
                    ),
                    {
                        "agent_id": agent_id,
                        "day": day,
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
        day = _utc_day_start(datetime.now(timezone.utc)).date()
        async with self._engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT usd_used, tokens_used FROM governance_cost_agent_daily "
                    "WHERE agent_id = :agent_id AND day_utc = :day"
                ),
                {"agent_id": agent_id, "day": day},
            )
            row = res.first()
        if row is None:
            return (0.0, 0)
        return (float(row[0]), int(row[1]))

    async def close(self) -> None:
        await self._engine.dispose()
