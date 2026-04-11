"""Agent presence module exposed via ``sdk.presence``.

Public API:
    await sdk.presence.heartbeat(agent_id)
    await sdk.presence.mark_idle(agent_id)
    await sdk.presence.close_agent(agent_id)
    await sdk.presence.list_agents()
    await sdk.presence.check_stale(timeout_seconds=300)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from .models import AgentStatus

logger = structlog.get_logger(__name__)


class PresenceModule:
    """Agent presence tracking — live/idle/unresponsive status."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        engine: Any = None,
    ) -> None:
        self._database_url = database_url
        self._engine: Any = engine
        self._owns_engine = False
        # In-memory fallback: {agent_id: {status, last_heartbeat, started_at, metadata}}
        self._agents: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _get_engine(self) -> Any:
        """Return the shared engine, or lazily create one if no shared engine was provided."""
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
        self._owns_engine = True
        return self._engine

    _MAX_AGENT_ID_LEN = 256
    _MAX_METADATA_BYTES = 65536
    _MAX_AGENTS = 10000

    async def heartbeat(
        self, agent_id: str, metadata: dict[str, Any] | None = None,
    ) -> None:
        """UPSERT agent as 'live' with current timestamp."""
        if not agent_id or len(agent_id) > self._MAX_AGENT_ID_LEN:
            logger.warning(
                "presence.heartbeat_invalid_agent_id",
                agent_id_len=len(agent_id) if agent_id else 0,
            )
            return
        if metadata is not None:
            import json as _json
            if len(_json.dumps(metadata)) > self._MAX_METADATA_BYTES:
                logger.warning("presence.heartbeat_metadata_too_large", agent_id=agent_id)
                return
        engine = self._get_engine()
        if engine is not None:
            await self._heartbeat_postgres(engine, agent_id, metadata)
        else:
            await self._heartbeat_memory(agent_id, metadata)

    async def _heartbeat_memory(
        self, agent_id: str, metadata: dict[str, Any] | None,
    ) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            existing = self._agents.get(agent_id)
            started = existing["started_at"] if existing else now
            self._agents[agent_id] = {
                "status": AgentStatus.LIVE.value,
                "last_heartbeat": now,
                "started_at": started,
                "metadata": metadata or {},
            }

    async def _heartbeat_postgres(
        self, engine: Any, agent_id: str, metadata: dict[str, Any] | None,
    ) -> None:
        import json

        from sqlalchemy import text

        meta_json = json.dumps(metadata or {})
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO governance_agent_presence "
                        "(agent_id, status, last_heartbeat, started_at, metadata_json) "
                        "VALUES (:agent_id, 'live', NOW(), NOW(), CAST(:meta AS jsonb)) "
                        "ON CONFLICT (agent_id) DO UPDATE SET "
                        "status = 'live', last_heartbeat = NOW(), "
                        "metadata_json = CAST(:meta AS jsonb)"
                    ),
                    {"agent_id": agent_id, "meta": meta_json},
                )
        except Exception as exc:
            logger.error(
                "presence.heartbeat_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )

    async def mark_idle(self, agent_id: str) -> None:
        """Update agent status to 'idle'."""
        engine = self._get_engine()
        if engine is not None:
            await self._mark_idle_postgres(engine, agent_id)
        else:
            await self._mark_idle_memory(agent_id)

    async def _mark_idle_memory(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id in self._agents:
                self._agents[agent_id]["status"] = AgentStatus.IDLE.value

    async def _mark_idle_postgres(self, engine: Any, agent_id: str) -> None:
        from sqlalchemy import text

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE governance_agent_presence "
                        "SET status = 'idle' "
                        "WHERE agent_id = :agent_id"
                    ),
                    {"agent_id": agent_id},
                )
        except Exception as exc:
            logger.error(
                "presence.mark_idle_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )

    async def close_agent(self, agent_id: str) -> None:
        """Remove agent from presence table."""
        engine = self._get_engine()
        if engine is not None:
            await self._close_agent_postgres(engine, agent_id)
        else:
            await self._close_agent_memory(agent_id)

    async def _close_agent_memory(self, agent_id: str) -> None:
        async with self._lock:
            self._agents.pop(agent_id, None)

    async def _close_agent_postgres(self, engine: Any, agent_id: str) -> None:
        from sqlalchemy import text

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM governance_agent_presence "
                        "WHERE agent_id = :agent_id"
                    ),
                    {"agent_id": agent_id},
                )
        except Exception as exc:
            logger.error(
                "presence.close_agent_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return all agents with their status."""
        engine = self._get_engine()
        if engine is not None:
            return await self._list_agents_postgres(engine)
        return await self._list_agents_memory()

    async def _list_agents_memory(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [
                {
                    "agent_id": aid,
                    "status": data["status"],
                    "last_heartbeat": data["last_heartbeat"].isoformat(),
                    "started_at": data["started_at"].isoformat(),
                    "metadata": data["metadata"],
                }
                for aid, data in self._agents.items()
            ]

    async def _list_agents_postgres(self, engine: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        try:
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT agent_id, status, last_heartbeat, started_at, metadata_json "
                        "FROM governance_agent_presence "
                        "ORDER BY agent_id"
                    )
                )
                rows = list(res.mappings())
            return [
                {
                    "agent_id": row["agent_id"],
                    "status": row["status"],
                    "last_heartbeat": row["last_heartbeat"].isoformat(),
                    "started_at": row["started_at"].isoformat(),
                    "metadata": row["metadata_json"],
                }
                for row in rows
            ]
        except Exception as exc:
            logger.error(
                "presence.list_agents_failed",
                error_type=type(exc).__name__,
            )
            return []

    async def check_stale(self, timeout_seconds: int = 300) -> None:
        """Mark agents as 'unresponsive' if heartbeat is older than timeout."""
        engine = self._get_engine()
        if engine is not None:
            await self._check_stale_postgres(engine, timeout_seconds)
        else:
            await self._check_stale_memory(timeout_seconds)

    async def _check_stale_memory(self, timeout_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            for agent_id, data in self._agents.items():
                elapsed = (now - data["last_heartbeat"]).total_seconds()
                if elapsed > timeout_seconds and data["status"] != AgentStatus.UNRESPONSIVE.value:
                    data["status"] = AgentStatus.UNRESPONSIVE.value

    async def _check_stale_postgres(
        self, engine: Any, timeout_seconds: int,
    ) -> None:
        from sqlalchemy import text

        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE governance_agent_presence "
                        "SET status = 'unresponsive' "
                        "WHERE last_heartbeat < NOW() - MAKE_INTERVAL(secs => :timeout) "
                        "AND status != 'unresponsive'"
                    ),
                    {"timeout": timeout_seconds},
                )
        except Exception as exc:
            logger.error(
                "presence.check_stale_failed",
                error_type=type(exc).__name__,
            )

    async def close(self) -> None:
        """Release resources. Disposes the engine only if this module owns it."""
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()
            self._engine = None
