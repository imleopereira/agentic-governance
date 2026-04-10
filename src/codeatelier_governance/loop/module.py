"""Loop / anomaly detection module exposed via ``sdk.loop``.

Public API:
    sdk.loop.register(policy)                              # at SDK init
    await sdk.loop.record_call(agent_id, session_id, tool) # record + check
    await sdk.loop.check(agent_id, session_id)             # read-only check

Every detection is auto-logged as an audit event with kind="loop.detected".
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from uuid import UUID

import structlog

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from .errors import LoopDetected
from .models import LoopPolicy

logger = structlog.get_logger(__name__)

# 24 hours in seconds for cleanup threshold
_CLEANUP_THRESHOLD_SECONDS = 86400


class LoopModule:
    """Loop / anomaly detection for repeated tool calls."""

    def __init__(
        self,
        audit: AuditModule,
        policies: list[LoopPolicy] | None = None,
        *,
        database_url: str | None = None,
    ) -> None:
        self._audit = audit
        self._policies: dict[str, LoopPolicy] = {}
        self._database_url = database_url
        self._engine: Any = None
        # In-memory tracking: {(agent_id, session_id): [(tool_name, timestamp)]}
        self._calls: dict[tuple[str, UUID], list[tuple[str, float]]] = {}
        self._lock = asyncio.Lock()
        for policy in policies or []:
            self._policies[policy.agent_id] = policy

    def _get_engine(self) -> Any:
        """Lazily create and return the SQLAlchemy async engine."""
        if self._engine is not None:
            return self._engine
        if self._database_url is None:
            return None
        from sqlalchemy.ext.asyncio import create_async_engine

        url = self._database_url
        if url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        elif not url.startswith("postgresql+asyncpg://"):
            return None
        self._engine = create_async_engine(
            url, pool_pre_ping=True, pool_size=2, max_overflow=5,
        )
        return self._engine

    def register(self, policy: LoopPolicy) -> None:
        """Register a loop policy for an agent. Call at app startup."""
        self._policies[policy.agent_id] = policy

    async def record_call(
        self,
        agent_id: str,
        session_id: UUID,
        tool_name: str,
    ) -> None:
        """Record a tool call and check for loops.

        If a loop is detected:
        - Emits a ``loop.detected`` audit event
        - Raises ``LoopDetected`` if the policy action is 'raise'
        """
        if not tool_name or len(tool_name) > 256:
            logger.warning(
                "loop.record_call_invalid_tool_name",
                agent_id=agent_id,
                tool_name_len=len(tool_name) if tool_name else 0,
            )
            return

        policy = self._policies.get(agent_id)
        if policy is None:
            return

        # Normalize to prevent case-sensitivity bypass (e.g. "read_file" vs "Read_File")
        tool_name = tool_name.lower()

        now = time.time()
        engine = self._get_engine()

        if engine is not None:
            await self._record_call_postgres(
                engine, agent_id, session_id, tool_name, policy, now,
            )
        else:
            await self._record_call_memory(
                agent_id, session_id, tool_name, policy, now,
            )

    async def _record_call_memory(
        self,
        agent_id: str,
        session_id: UUID,
        tool_name: str,
        policy: LoopPolicy,
        now: float,
    ) -> None:
        """Record call in memory and check for loops."""
        cutoff = now - policy.window_seconds
        async with self._lock:
            key = (agent_id, session_id)
            calls = self._calls.get(key, [])
            # Prune entries older than window
            calls = [(t, ts) for t, ts in calls if ts >= cutoff]
            calls.append((tool_name, now))
            self._calls[key] = calls

            # Opportunistic cleanup: prune all sessions for old entries
            cleanup_cutoff = now - _CLEANUP_THRESHOLD_SECONDS
            keys_to_clean = [
                k for k in self._calls
                if k != key and self._calls[k] and self._calls[k][-1][1] < cleanup_cutoff
            ]
            for k in keys_to_clean:
                del self._calls[k]

            # Count calls for this tool within window
            count = sum(1 for t, ts in calls if t == tool_name)

        if count > policy.max_calls:
            await self._handle_detection(
                agent_id, session_id, tool_name, count, policy,
            )

    async def _record_call_postgres(
        self,
        engine: Any,
        agent_id: str,
        session_id: UUID,
        tool_name: str,
        policy: LoopPolicy,
        now: float,
    ) -> None:
        """Record call in Postgres and check for loops."""
        from sqlalchemy import text

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO governance_loop_tracking "
                        "(session_id, agent_id, tool_name, called_at) "
                        "VALUES (:session_id, :agent_id, :tool_name, NOW())"
                    ),
                    {
                        "session_id": str(session_id),
                        "agent_id": agent_id,
                        "tool_name": tool_name,
                    },
                )
                # Opportunistic cleanup with probability 1/100
                if random.randint(1, 100) == 1:  # noqa: S311
                    await conn.execute(
                        text(
                            "DELETE FROM governance_loop_tracking "
                            "WHERE called_at < NOW() - INTERVAL '24 hours'"
                        )
                    )

            # Check for loop
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM governance_loop_tracking "
                        "WHERE session_id = :session_id "
                        "AND tool_name = :tool_name "
                        "AND called_at >= NOW() - MAKE_INTERVAL(secs => :window)"
                    ),
                    {
                        "session_id": str(session_id),
                        "tool_name": tool_name,
                        "window": policy.window_seconds,
                    },
                )
                row = res.first()
                count = int(row[0]) if row else 0

        except Exception as exc:
            logger.error(
                "loop.record_call_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )
            return

        if count > policy.max_calls:
            await self._handle_detection(
                agent_id, session_id, tool_name, count, policy,
            )

    async def check(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> None:
        """Read-only check for loops without recording a call.

        Raises ``LoopDetected`` if any tool exceeds the policy threshold.
        """
        policy = self._policies.get(agent_id)
        if policy is None:
            return

        engine = self._get_engine()
        if engine is not None:
            await self._check_postgres(engine, agent_id, session_id, policy)
        else:
            await self._check_memory(agent_id, session_id, policy)

    async def _check_memory(
        self,
        agent_id: str,
        session_id: UUID,
        policy: LoopPolicy,
    ) -> None:
        """Check in-memory for loops."""
        now = time.time()
        cutoff = now - policy.window_seconds
        async with self._lock:
            key = (agent_id, session_id)
            calls = self._calls.get(key, [])
            # Count per tool within window
            tool_counts: dict[str, int] = {}
            for t, ts in calls:
                if ts >= cutoff:
                    tool_counts[t] = tool_counts.get(t, 0) + 1

        for tool_name, count in tool_counts.items():
            if count > policy.max_calls:
                await self._handle_detection(
                    agent_id, session_id, tool_name, count, policy,
                )
                return  # Stop after first detection

    async def _check_postgres(
        self,
        engine: Any,
        agent_id: str,
        session_id: UUID,
        policy: LoopPolicy,
    ) -> None:
        """Check Postgres for loops."""
        from sqlalchemy import text

        try:
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT tool_name, COUNT(*) as cnt "
                        "FROM governance_loop_tracking "
                        "WHERE session_id = :session_id "
                        "AND called_at >= NOW() - MAKE_INTERVAL(secs => :window) "
                        "GROUP BY tool_name "
                        "HAVING COUNT(*) > :max_calls"
                    ),
                    {
                        "session_id": str(session_id),
                        "window": policy.window_seconds,
                        "max_calls": policy.max_calls,
                    },
                )
                row = res.first()
        except Exception as exc:
            logger.error(
                "loop.check_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )
            return

        if row is not None:
            await self._handle_detection(
                agent_id, session_id, row[0], int(row[1]), policy,
            )

    async def _handle_detection(
        self,
        agent_id: str,
        session_id: UUID,
        tool_name: str,
        count: int,
        policy: LoopPolicy,
    ) -> None:
        """Emit audit event and optionally raise."""
        await self._audit.log(
            AuditEvent(
                agent_id=agent_id,
                session_id=session_id,
                kind="loop.detected",
                metadata={
                    "tool_name": tool_name,
                    "count": count,
                    "window_seconds": policy.window_seconds,
                    "max_calls": policy.max_calls,
                    "action": policy.action,
                },
            )
        )
        if policy.action == "raise":
            raise LoopDetected(
                f"loop detected: tool={tool_name!r} called {count} times "
                f"in {policy.window_seconds}s window (max {policy.max_calls}) "
                f"for agent_id={agent_id!r}"
            )

    async def close(self) -> None:
        """Release resources."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
