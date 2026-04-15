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
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from .errors import AgentKilledError
from .models import AgentStatus

logger = structlog.get_logger(__name__)

# Kill-switch cache TTL: 5 seconds.
# Trade-off: bounds DB load (max 1 query per 5s per host process) at the cost
# of a worst-case 5-second delay between an operator clicking Kill in the
# console and the SDK fail-closing on that agent. Hotfix v0.5.4 default.
_KILL_CACHE_TTL_SECONDS = 5.0


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
        # Kill-switch cache: agent_id -> kill metadata dict.
        # Refreshed from DB on stale read. See _refresh_killed_cache below.
        # Designed for Invariant #1: if the DB is unreachable, _killed_cache
        # holds the last known good state so already-killed agents stay killed
        # and live agents stay live, instead of crashing the host application.
        self._killed_cache: dict[str, dict[str, Any]] = {}
        self._killed_cache_at: float = 0.0
        self._killed_lock = asyncio.Lock()

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

    _MAX_OPERATOR_ID_LEN = 256

    async def heartbeat(
        self,
        agent_id: str,
        metadata: dict[str, Any] | None = None,
        operator_id: str | None = None,
    ) -> None:
        """UPSERT agent as 'live' with current timestamp.

        Args:
            agent_id: Unique identifier for the agent.
            metadata: Optional JSON-serialisable metadata dict.
            operator_id: Optional ID of the human operator who owns this agent.
                Used by the console to enforce self-approval prevention on HITL
                gates.
        """
        if not agent_id or len(agent_id) > self._MAX_AGENT_ID_LEN:
            logger.warning(
                "presence.heartbeat_invalid_agent_id",
                agent_id_len=len(agent_id) if agent_id else 0,
            )
            return
        if operator_id is not None and len(operator_id) > self._MAX_OPERATOR_ID_LEN:
            logger.warning(
                "presence.heartbeat_invalid_operator_id",
                agent_id=agent_id,
                operator_id_len=len(operator_id),
            )
            return
        if metadata is not None:
            import json as _json
            if len(_json.dumps(metadata)) > self._MAX_METADATA_BYTES:
                logger.warning("presence.heartbeat_metadata_too_large", agent_id=agent_id)
                return
        engine = self._get_engine()
        if engine is not None:
            await self._heartbeat_postgres(engine, agent_id, metadata, operator_id)
        else:
            await self._heartbeat_memory(agent_id, metadata, operator_id)

    async def _heartbeat_memory(
        self,
        agent_id: str,
        metadata: dict[str, Any] | None,
        operator_id: str | None,
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
                "operator_id": operator_id,
            }

    async def _heartbeat_postgres(
        self,
        engine: Any,
        agent_id: str,
        metadata: dict[str, Any] | None,
        operator_id: str | None,
    ) -> None:
        import json

        from sqlalchemy import text

        meta_json = json.dumps(metadata or {})
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO governance_agent_presence "
                        "(agent_id, status, last_heartbeat, started_at, metadata_json, operator_id) "
                        "VALUES (:agent_id, 'live', NOW(), NOW(), CAST(:meta AS jsonb), :operator_id) "
                        "ON CONFLICT (agent_id) DO UPDATE SET "
                        "status = 'live', last_heartbeat = NOW(), "
                        "metadata_json = CAST(:meta AS jsonb), "
                        "operator_id = :operator_id"
                    ),
                    {"agent_id": agent_id, "meta": meta_json, "operator_id": operator_id},
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
                    "operator_id": data.get("operator_id"),
                }
                for aid, data in self._agents.items()
            ]

    async def _list_agents_postgres(self, engine: Any) -> list[dict[str, Any]]:
        from sqlalchemy import text

        try:
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT agent_id, status, last_heartbeat, started_at, "
                        "metadata_json, operator_id "
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
                    "operator_id": row["operator_id"],
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

    # ------------------------------------------------------------------
    # Kill switch (v0.5.4 hotfix)
    # ------------------------------------------------------------------
    #
    # The kill switch is the single fail-closed control an operator has over
    # a running agent. When an operator clicks "Kill" in the console
    # (POST /api/agents/{agent_id}/kill), the console writes a marker into
    # `governance_agent_presence.metadata_json` containing `_killed_by`,
    # `_killed_at`, `_kill_reason`. THIS module is the SDK-side enforcement
    # of that marker.
    #
    # IMPORTANT: as of v0.5.4 we do NOT use `status='unresponsive'` to detect
    # kills, because `check_stale()` also writes that status for heartbeat
    # timeouts. Conflating the two would block agents that simply went idle.
    # The metadata marker is the unambiguous kill signal.
    #
    # Cache strategy: 5-second TTL in-memory dict. On every is_killed() /
    # assert_alive() call, if the cache is older than TTL, refresh from DB.
    # If the DB is unreachable (Invariant #1), the cache holds the last known
    # state — already-killed agents stay killed, live agents stay live, and
    # the host application keeps running. A WARNING is logged on DB error
    # but no exception propagates.
    #
    # Why on PresenceModule and not a separate KillSwitchModule: presence
    # already owns `governance_agent_presence` and has the engine wiring.
    # Adding a second module for one column-read would double the DB-engine
    # lifecycle surface area for zero gain.

    async def is_killed(self, agent_id: str) -> bool:
        """Return True if the agent has been killed by an operator.

        Reads from a 5-second TTL cache. On cache miss or stale, refreshes
        from `governance_agent_presence` looking for any row where
        `metadata_json->>'_killed_by'` IS NOT NULL. On DB error, falls back
        to the existing cache (Invariant #1: never crashes the host).

        This method is the source of truth for kill state inside the SDK.
        Called by:
          * scope.check() top-of-function
          * cost.check_budget() top-of-function
          * gates.check() top-of-function
          * any wrapped LLM client (wrap_anthropic / wrap_openai) before send
        """
        await self._maybe_refresh_killed_cache()
        return agent_id in self._killed_cache

    async def assert_alive(self, agent_id: str) -> None:
        """Raise AgentKilledError if the agent has been killed.

        Convenience wrapper over is_killed() that includes the kill metadata
        in the exception so callers don't have to look it up separately.

        Raises:
            AgentKilledError: the agent is in the killed-cache.
        """
        await self._maybe_refresh_killed_cache()
        kill_meta = self._killed_cache.get(agent_id)
        if kill_meta is not None:
            raise AgentKilledError(
                agent_id,
                killed_by=kill_meta.get("killed_by"),
                killed_at=kill_meta.get("killed_at"),
                reason=kill_meta.get("reason"),
            )

    async def _maybe_refresh_killed_cache(self) -> None:
        """Refresh the killed-agent cache from DB if older than TTL.

        Locked so concurrent callers only trigger one DB query per refresh.
        Best-effort: on DB error, leaves the existing cache in place and
        logs. Never raises (Invariant #1).
        """
        now = time.monotonic()
        if (now - self._killed_cache_at) < _KILL_CACHE_TTL_SECONDS:
            return
        async with self._killed_lock:
            # Double-check after acquiring the lock — another coroutine may
            # have refreshed while we were waiting.
            now = time.monotonic()
            if (now - self._killed_cache_at) < _KILL_CACHE_TTL_SECONDS:
                return
            engine = self._get_engine()
            if engine is None:
                # No DB configured (in-memory mode for tests). Use the
                # in-memory _agents dict as the source of truth.
                self._killed_cache = self._derive_killed_from_memory()
                self._killed_cache_at = now
                return
            try:
                fresh = await self._fetch_killed_from_postgres(engine)
            except Exception as exc:
                logger.warning(
                    "presence.killed_cache_refresh_failed",
                    error_type=type(exc).__name__,
                    cached_count=len(self._killed_cache),
                )
                # Keep the existing cache. Bump the timestamp so we don't
                # hammer the DB during an outage; we'll retry next TTL.
                self._killed_cache_at = now
                return
            self._killed_cache = fresh
            self._killed_cache_at = now

    def _derive_killed_from_memory(self) -> dict[str, dict[str, Any]]:
        """Extract kill markers from the in-memory _agents fallback."""
        out: dict[str, dict[str, Any]] = {}
        for aid, data in self._agents.items():
            meta = data.get("metadata") or {}
            killed_by = meta.get("_killed_by")
            if killed_by is None:
                continue
            out[aid] = {
                "killed_by": killed_by,
                "killed_at": meta.get("_killed_at"),
                "reason": meta.get("_kill_reason"),
            }
        return out

    async def _fetch_killed_from_postgres(
        self, engine: Any
    ) -> dict[str, dict[str, Any]]:
        """Query DB for all currently-killed agents.

        SELECT agent_id + the three kill metadata fields. Filters on
        metadata_json->>'_killed_by' IS NOT NULL — the marker the console
        writes in `kill_agent` (app.py:1788-1802).
        """
        from sqlalchemy import text

        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, "
                    "       metadata_json->>'_killed_by' AS killed_by, "
                    "       metadata_json->>'_killed_at' AS killed_at, "
                    "       metadata_json->>'_kill_reason' AS reason "
                    "FROM governance_agent_presence "
                    "WHERE metadata_json->>'_killed_by' IS NOT NULL"
                )
            )
            rows = list(res.mappings())
        return {
            row["agent_id"]: {
                "killed_by": row["killed_by"],
                "killed_at": row["killed_at"],
                "reason": row["reason"],
            }
            for row in rows
        }

    async def force_refresh_killed_cache(self) -> None:
        """Force an immediate refresh of the killed-cache, bypassing TTL.

        Used by tests and by future LISTEN/NOTIFY-driven invalidation
        (out of scope for v0.5.4 — that's v0.6 work).
        """
        self._killed_cache_at = 0.0
        await self._maybe_refresh_killed_cache()

    async def close(self) -> None:
        """Release resources. Disposes the engine only if this module owns it."""
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()
            self._engine = None
