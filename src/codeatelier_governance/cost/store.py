"""Cost store abstraction + in-memory implementation.

Two implementations:
    InMemoryCostStore — single-process, used for tests and local dev
    PostgresCostStore — multi-process correct (see postgres_store.py)

The store is responsible for atomic counter updates. Concurrent ``track``
calls from multiple workers MUST sum correctly without lost updates.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from uuid import UUID


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class CostStore(ABC):
    """Abstract cost storage backend."""

    @abstractmethod
    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int,
        usd: float,
    ) -> None:
        """Atomically add ``tokens`` and ``usd`` to the session AND daily counters.

        Implementations MUST be safe under concurrent calls from multiple
        workers. The Postgres implementation uses ``ON CONFLICT DO UPDATE
        SET col = col + EXCLUDED.col`` for row-level atomicity.
        """

    @abstractmethod
    async def get_session_usage(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> tuple[float, int]:
        """Return ``(usd_used, tokens_used)`` for the (agent, session) pair."""

    @abstractmethod
    async def get_agent_daily_usage(
        self,
        agent_id: str,
    ) -> tuple[float, int]:
        """Return ``(usd_used, tokens_used)`` for the agent's current UTC day."""

    async def get_session_start_time(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> datetime | None:
        """Return the timestamp of the first track() call for this session, or None."""
        return None

    async def close(self) -> None:
        """Release resources. Override if needed."""
        return None


class InMemoryCostStore(CostStore):
    """In-memory cost store. Single-process correct only."""

    def __init__(self) -> None:
        self._session_usage: dict[tuple[str, UUID], tuple[float, int]] = {}
        self._session_started: dict[tuple[str, UUID], datetime] = {}
        self._agent_daily: dict[str, tuple[float, int, datetime]] = {}
        self._lock = asyncio.Lock()

    async def track(
        self,
        agent_id: str,
        session_id: UUID,
        *,
        tokens: int,
        usd: float,
    ) -> None:
        now = datetime.now(timezone.utc)
        day = _utc_day_start(now)
        async with self._lock:
            key = (agent_id, session_id)
            if key not in self._session_started:
                self._session_started[key] = now
            cur_usd, cur_tok = self._session_usage.get(key, (0.0, 0))
            self._session_usage[key] = (
                cur_usd + usd,
                cur_tok + tokens,
            )
            d_usd, d_tok, d_day = self._agent_daily.get(agent_id, (0.0, 0, day))
            if d_day < day:
                d_usd, d_tok, d_day = 0.0, 0, day
            self._agent_daily[agent_id] = (d_usd + usd, d_tok + tokens, d_day)

    async def get_session_usage(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> tuple[float, int]:
        async with self._lock:
            return self._session_usage.get((agent_id, session_id), (0.0, 0))

    async def get_agent_daily_usage(
        self,
        agent_id: str,
    ) -> tuple[float, int]:
        now = datetime.now(timezone.utc)
        day = _utc_day_start(now)
        async with self._lock:
            d_usd, d_tok, d_day = self._agent_daily.get(agent_id, (0.0, 0, day))
            if d_day < day:
                return (0.0, 0)
            return (d_usd, d_tok)

    async def get_session_start_time(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> datetime | None:
        async with self._lock:
            return self._session_started.get((agent_id, session_id))
