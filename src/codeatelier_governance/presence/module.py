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

from .errors import AgentHaltedError, HaltPersistenceError
from .models import AgentStatus

logger = structlog.get_logger(__name__)

# Halt-switch cache TTL: 5 seconds.
# Trade-off: bounds DB load (max 1 query per 5s per host process) at the cost
# of a worst-case 5-second delay between an operator clicking Halt in the
# console and the SDK fail-closing on that agent. Hotfix v0.5.4 default,
# renamed from `_KILL_CACHE_TTL_SECONDS` in v0.6 (F2.5).
_HALT_CACHE_TTL_SECONDS = 5.0

# Backward-compat alias (removed in v0.7). v0.5.x code that reaches into the
# module's private constants continues to work; new code uses `_HALT_*`.
_KILL_CACHE_TTL_SECONDS = _HALT_CACHE_TTL_SECONDS


class PresenceModule:
    """Agent presence tracking — live/idle/unresponsive status."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        engine: Any = None,
        halt_engine: Any = None,
    ) -> None:
        self._database_url = database_url
        self._engine: Any = engine
        self._owns_engine = False
        # Optional PRIVILEGED engine used ONLY for the halt WRITE path. When
        # the self-unhalt REVOKE is applied (ddl.sql), the shared app/agent
        # engine cannot UPDATE the halt-marker columns, so halt() must run
        # under a role that can. When None, halt() falls back to the shared
        # engine (correct for single-role deployments that do NOT apply the
        # REVOKE). Never used for reads or the agent hot path.
        self._halt_engine: Any = halt_engine
        # In-memory fallback: {agent_id: {status, last_heartbeat, started_at, metadata}}
        self._agents: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        # Halt-switch cache: agent_id -> halt metadata dict.
        # Refreshed from DB on stale read. See _maybe_refresh_halted_cache below.
        # Designed for Invariant #1: if the DB is unreachable, _halted_cache
        # holds the last known good state so already-halted agents stay halted
        # and live agents stay live, instead of crashing the host application.
        self._halted_cache: dict[str, dict[str, Any]] = {}
        self._halted_cache_at: float = 0.0
        self._halted_lock = asyncio.Lock()

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

    def _get_halt_engine(self) -> Any:
        """Return the privileged halt engine if configured, else the shared engine.

        The halt WRITE runs here so it can use a role that retains UPDATE on
        the halt-marker columns even when the agent's shared role has them
        REVOKE'd. Reads and the enforcement hot path always use the shared
        engine (_get_engine).
        """
        if self._halt_engine is not None:
            return self._halt_engine
        return self._get_engine()

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
            operator_id: Optional ID of the human operator who owns this
                agent. Recorded for provenance and surfaced by list_agents();
                the SDK does not use it to enforce any approver-identity gate.
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
            # SECURITY (self-unhalt defense): carry the halt marker forward
            # like started_at. The heartbeat rewrites 'metadata' wholesale,
            # so the marker must live at the top level — outside 'metadata'
            # — exactly as it lives in dedicated DB columns the heartbeat
            # UPSERT never touches. A heartbeat must never clear a halt.
            self._agents[agent_id] = {
                "status": AgentStatus.LIVE.value,
                "last_heartbeat": now,
                "started_at": started,
                "metadata": metadata or {},
                "operator_id": operator_id,
                "halted_by": existing.get("halted_by") if existing else None,
                "halted_at": existing.get("halted_at") if existing else None,
                "halt_reason": existing.get("halt_reason") if existing else None,
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
                        # SECURITY (self-unhalt defense): halted_by /
                        # halted_at / halt_reason are intentionally NOT in
                        # this SET list, so a heartbeat can never clear a
                        # halt. The app/agent role also has UPDATE on those
                        # columns revoked at the grant level (see ddl.sql).
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
            existing = self._agents.get(agent_id)
            # SECURITY (self-unhalt defense): a halted agent is NOT removed,
            # mirroring the DB trigger, so it cannot close-then-heartbeat to
            # clear its own halt. Clear the halt first to close it.
            if existing is not None and existing.get("halted_by") is not None:
                return
            self._agents.pop(agent_id, None)

    async def _close_agent_postgres(self, engine: Any, agent_id: str) -> None:
        from sqlalchemy import text

        try:
            async with engine.begin() as conn:
                # SECURITY (self-unhalt defense): only delete a NON-halted
                # row, so a halted agent cannot close-then-heartbeat to clear
                # its own halt. A DB trigger enforces this against raw DELETE.
                await conn.execute(
                    text(
                        "DELETE FROM governance_agent_presence "
                        "WHERE agent_id = :agent_id AND halted_by IS NULL"
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

    async def halt(
        self,
        agent_id: str,
        *,
        halted_by: str,
        reason: str | None = None,
    ) -> None:
        """Write the operator halt marker for ``agent_id``.

        Records an operator-initiated halt. The marker is written to the
        dedicated ``halted_by`` / ``halted_at`` / ``halt_reason`` columns —
        NOT into ``metadata_json`` — so the agent's own heartbeat (which
        rewrites ``metadata_json`` wholesale) can never erase it, and a
        halted row cannot be deleted. See the self-unhalt defense notes in
        ``ddl.sql``.

        Deployment (see ddl.sql for the two supported models):

          * Single-role (agent cannot issue raw SQL): do NOT apply the
            self-unhalt column REVOKE. The write runs under the shared engine;
            self-unhalt is still blocked by the BEFORE DELETE trigger and by
            the fact that no SDK method clears a halt.
          * Hardened / multi-role (agent CAN issue raw SQL): apply the column
            REVOKE to the agent role and give this module a PRIVILEGED
            ``halt_engine`` (``presence_halt_database_url`` at the SDK level).
            The agent role then cannot self-unhalt, and this write runs under
            the privileged role that can.

        If the REVOKE is applied but no privileged ``halt_engine`` is
        configured, the write is denied and this method raises
        :class:`HaltPersistenceError` — it never returns a false success on a
        kill-switch write.

        Args:
            agent_id: Unique identifier for the agent to halt.
            halted_by: Operator identity recorded on the marker.
            reason: Optional free-text halt reason.

        Raises:
            HaltPersistenceError: the halt write failed or was denied, so the
                halt did NOT engage.
        """
        engine = self._get_halt_engine()
        if engine is not None:
            await self._halt_postgres(engine, agent_id, halted_by, reason)
        else:
            await self._halt_memory(agent_id, halted_by, reason)

    async def _halt_memory(
        self,
        agent_id: str,
        halted_by: str,
        reason: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            data = self._agents.get(agent_id)
            if data is None:
                # Halt a not-yet-seen agent so the marker is in place the
                # moment it first heartbeats. Mirrors the DB path, which
                # would no-op on a missing row; here we materialise a
                # minimal row carrying only the marker.
                now_dt = datetime.now(timezone.utc)
                data = {
                    "status": AgentStatus.LIVE.value,
                    "last_heartbeat": now_dt,
                    "started_at": now_dt,
                    "metadata": {},
                    "operator_id": None,
                }
                self._agents[agent_id] = data
            # Marker lives at the top level, outside 'metadata', so the
            # heartbeat's wholesale metadata rewrite cannot clear it.
            data["halted_by"] = halted_by
            data["halted_at"] = now
            data["halt_reason"] = reason

    async def _halt_postgres(
        self,
        engine: Any,
        agent_id: str,
        halted_by: str,
        reason: str | None,
    ) -> None:
        from sqlalchemy import text

        try:
            async with engine.begin() as conn:
                # UPSERT, not a bare UPDATE. An operator may halt an agent
                # that has not heartbeated yet (no row) or whose row is being
                # deleted by a concurrent close_agent(); a plain UPDATE would
                # affect 0 rows. ON CONFLICT re-halts an existing row. This
                # runs under the halt engine (privileged when the self-unhalt
                # REVOKE is applied; the shared engine otherwise).
                result = await conn.execute(
                    text(
                        "INSERT INTO governance_agent_presence "
                        "(agent_id, halted_by, halted_at, halt_reason) "
                        "VALUES (:aid, :by, NOW(), :reason) "
                        "ON CONFLICT (agent_id) DO UPDATE SET "
                        "halted_by = :by, halted_at = NOW(), "
                        "halt_reason = :reason"
                    ),
                    {"by": halted_by, "reason": reason, "aid": agent_id},
                )
                rowcount = result.rowcount
        except Exception as exc:
            # A kill-switch write that fails MUST be loud, never a false
            # success. The usual cause under the hardened config is a denied
            # UPDATE on the halt columns (the agent role is REVOKE'd and no
            # privileged halt engine was configured). Raise so the operator
            # knows the halt did NOT engage. (halt() is operator-facing, off
            # the agent hot path, so raising here does not violate Invariant #1.)
            logger.error(
                "presence.halt_failed",
                error_type=type(exc).__name__,
                agent_id=agent_id,
            )
            raise HaltPersistenceError(
                f"Failed to persist halt for agent {agent_id!r} "
                f"({type(exc).__name__}). If the self-unhalt REVOKE is applied, "
                f"halt() must run under a privileged connection "
                f"(PresenceModule(halt_engine=...) / presence_halt_database_url)."
            ) from exc
        if rowcount == 0:
            # Unreachable for an UPSERT (INSERT-or-UPDATE always touches a row);
            # a 0-row result means the marker did not persist.
            logger.error(
                "presence.halt_not_persisted",
                agent_id=agent_id,
                halted_by=halted_by,
            )
            raise HaltPersistenceError(
                f"Halt for agent {agent_id!r} affected 0 rows; the marker did "
                f"not persist."
            )

    # ------------------------------------------------------------------
    # Halt switch (fail-closed operator kill switch)
    # ------------------------------------------------------------------
    #
    # The halt switch is the single fail-closed control an operator has over
    # a running agent. An operator halt (see :meth:`halt`, run under the halt
    # engine — a privileged connection in a hardened deployment) writes the
    # marker into the dedicated `governance_agent_presence.halted_by` /
    # `halted_at` / `halt_reason` columns. THIS module is the SDK-side
    # enforcement of that marker.
    #
    # SECURITY (self-unhalt defense): the marker lives ONLY in those
    # dedicated columns, never in `metadata_json`. The agent's own heartbeat
    # rewrites `metadata_json` wholesale on every beat, so a marker stored
    # there could be erased by the very agent it is meant to stop. The
    # columns are unwritable by the app/agent role (column-level REVOKE) and
    # a halted row cannot be DELETEd (BEFORE DELETE trigger), so neither a
    # heartbeat nor a close-then-reinsert can clear a halt. The legacy
    # metadata-key representation (`_halted_*` / `_killed_*`) was read for
    # back-compat through v0.6 and removed in v0.7; migration a7f2haltcols
    # backfilled any such markers into the columns.
    #
    # NOTE: we do NOT use `status='unresponsive'` to detect halts, because
    # `check_stale()` also writes that status for heartbeat timeouts.
    # Conflating the two would block agents that simply went idle.
    #
    # Cache strategy: 5-second TTL in-memory dict. On every is_halted() /
    # assert_not_halted() call, if the cache is older than TTL, refresh from
    # DB. If the DB is unreachable (Invariant #1), the cache holds the last
    # known state — already-halted agents stay halted, live agents stay live,
    # and the host application keeps running. A WARNING is logged on DB
    # error but no exception propagates.

    async def is_halted(self, agent_id: str) -> bool:
        """Return True if the agent has been halted by an operator.

        Reads from a 5-second TTL cache. On cache miss or stale, refreshes
        from `governance_agent_presence` looking for any row whose dedicated
        `halted_by` column is set. On DB error, falls back to the existing
        cache (Invariant #1: never crashes the host).

        This method is the source of truth for halt state inside the SDK.
        Called by:
          * scope.check() top-of-function
          * cost.check_budget() top-of-function
          * gates.check() top-of-function
          * any wrapped LLM client (wrap_anthropic / wrap_openai) before send
        """
        await self._maybe_refresh_halted_cache()
        return agent_id in self._halted_cache

    async def assert_not_halted(self, agent_id: str) -> None:
        """Raise AgentHaltedError if the agent has been halted.

        Convenience wrapper over is_halted() that includes the halt metadata
        in the exception so callers don't have to look it up separately.

        Raises:
            AgentHaltedError: the agent is in the halted-cache.
        """
        await self._maybe_refresh_halted_cache()
        halt_meta = self._halted_cache.get(agent_id)
        if halt_meta is not None:
            raise AgentHaltedError(
                agent_id,
                halted_by=halt_meta.get("halted_by"),
                halted_at=halt_meta.get("halted_at"),
                reason=halt_meta.get("reason"),
            )

    async def _maybe_refresh_halted_cache(self) -> None:
        """Refresh the halted-agent cache from DB if older than TTL.

        Locked so concurrent callers only trigger one DB query per refresh.
        Best-effort: on DB error, leaves the existing cache in place and
        logs. Never raises (Invariant #1).
        """
        now = time.monotonic()
        if (now - self._halted_cache_at) < _HALT_CACHE_TTL_SECONDS:
            return
        async with self._halted_lock:
            # Double-check after acquiring the lock — another coroutine may
            # have refreshed while we were waiting.
            now = time.monotonic()
            if (now - self._halted_cache_at) < _HALT_CACHE_TTL_SECONDS:
                return
            engine = self._get_engine()
            if engine is None:
                # No DB configured (in-memory mode for tests). Use the
                # in-memory _agents dict as the source of truth.
                self._halted_cache = self._derive_halted_from_memory()
                self._halted_cache_at = now
                return
            try:
                fresh = await self._fetch_halted_from_postgres(engine)
            except Exception as exc:
                logger.warning(
                    "presence.halted_cache_refresh_failed",
                    error_type=type(exc).__name__,
                    cached_count=len(self._halted_cache),
                )
                # Keep the existing cache. Bump the timestamp so we don't
                # hammer the DB during an outage; we'll retry next TTL.
                self._halted_cache_at = now
                return
            self._halted_cache = fresh
            self._halted_cache_at = now

    def _derive_halted_from_memory(self) -> dict[str, dict[str, Any]]:
        """Extract halt markers from the in-memory _agents fallback.

        Reads ONLY the dedicated top-level ``halted_by`` / ``halted_at`` /
        ``halt_reason`` keys written by :meth:`halt` and carried across
        heartbeats (the in-memory analogue of the revoke-protected DB
        columns). The legacy ``metadata`` fallback (``_halted_*`` /
        ``_killed_*``) was removed in v0.7: the heartbeat rewrites
        ``metadata`` wholesale, which made a marker stored there
        self-clearable by the halted agent.
        """
        out: dict[str, dict[str, Any]] = {}
        for aid, data in self._agents.items():
            halted_by = data.get("halted_by")
            if halted_by is None:
                continue
            out[aid] = {
                "halted_by": halted_by,
                "halted_at": data.get("halted_at"),
                "reason": data.get("halt_reason"),
            }
        return out

    async def _fetch_halted_from_postgres(
        self, engine: Any
    ) -> dict[str, dict[str, Any]]:
        """Query DB for all currently-halted agents.

        Reads ONLY the dedicated, revoke-protected ``halted_by`` /
        ``halted_at`` / ``halt_reason`` columns written by :meth:`halt`.

        The legacy metadata-key fallback (``_halted_*`` / ``_killed_*``)
        was removed in v0.7. It was a self-unhalt vector: the agent's own
        heartbeat rewrites ``metadata_json`` wholesale, so a halt marker
        stored there could be cleared by the very agent it was meant to
        stop. Migration ``a7f2haltcols`` backfilled any pre-existing
        metadata markers into these columns, so nothing is lost — the halt
        now lives only where the app/agent role cannot write it.
        """
        from sqlalchemy import text

        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, halted_by, "
                    "       halted_at::text AS halted_at, "
                    "       halt_reason AS reason "
                    "FROM governance_agent_presence "
                    "WHERE halted_by IS NOT NULL"
                )
            )
            rows = list(res.mappings())
        return {
            row["agent_id"]: {
                "halted_by": row["halted_by"],
                "halted_at": row["halted_at"],
                "reason": row["reason"],
            }
            for row in rows
        }

    async def force_refresh_halted_cache(self) -> None:
        """Force an immediate refresh of the halted-cache, bypassing TTL.

        Used by tests and by future LISTEN/NOTIFY-driven invalidation.
        """
        self._halted_cache_at = 0.0
        await self._maybe_refresh_halted_cache()

    # ------------------------------------------------------------------
    # Backward-compat aliases (v0.5.x → v0.6). Removed in v0.7.
    # ------------------------------------------------------------------
    #
    # These keep the old `kill` / `killed` / `alive` vocabulary working
    # for exactly one release. The goal is that v0.5.x wrappers, tests,
    # and operator tooling that call `presence.is_killed(...)` or
    # `presence.assert_alive(...)` continue to work unchanged when the
    # library is upgraded to v0.6 — they forward directly to the new
    # halt-named methods. v0.7 deletes these aliases.
    #
    # Keeping the aliases as ordinary (non-wrapped) method references
    # means there is NO runtime warning on every call — a warning fired
    # on every enforcement check would flood logs in any app that's on
    # the hot path. The deprecation is announced via CHANGELOG + the
    # deprecation HTTP header on the /kill route instead.

    async def is_killed(self, agent_id: str) -> bool:
        """Deprecated alias of :meth:`is_halted`. Removed in v0.7."""
        return await self.is_halted(agent_id)

    async def assert_alive(self, agent_id: str) -> None:
        """Deprecated alias of :meth:`assert_not_halted`. Removed in v0.7."""
        await self.assert_not_halted(agent_id)

    async def force_refresh_killed_cache(self) -> None:
        """Deprecated alias of :meth:`force_refresh_halted_cache`. Removed in v0.7."""
        await self.force_refresh_halted_cache()

    async def _maybe_refresh_killed_cache(self) -> None:
        """Deprecated alias of :meth:`_maybe_refresh_halted_cache`. Removed in v0.7."""
        await self._maybe_refresh_halted_cache()

    async def _fetch_killed_from_postgres(
        self, engine: Any
    ) -> dict[str, dict[str, Any]]:
        """Deprecated alias of :meth:`_fetch_halted_from_postgres`. Removed in v0.7.

        Returned dicts use the v0.5.x key names (``killed_by`` / ``killed_at``)
        so tests that assert on the old cache shape keep passing.
        """
        fresh = await self._fetch_halted_from_postgres(engine)
        return {
            aid: {
                "killed_by": entry.get("halted_by"),
                "killed_at": entry.get("halted_at"),
                "reason": entry.get("reason"),
            }
            for aid, entry in fresh.items()
        }

    def _derive_killed_from_memory(self) -> dict[str, dict[str, Any]]:
        """Deprecated alias of :meth:`_derive_halted_from_memory`. Removed in v0.7.

        Returned dicts still use the v0.5.x key names (``killed_by`` /
        ``killed_at``) so tests that compare cache contents against the
        v0.5.4 schema keep passing.
        """
        out: dict[str, dict[str, Any]] = {}
        for aid, entry in self._derive_halted_from_memory().items():
            out[aid] = {
                "killed_by": entry.get("halted_by"),
                "killed_at": entry.get("halted_at"),
                "reason": entry.get("reason"),
            }
        return out

    # Back-compat attribute aliases for the private cache. v0.5.x tests
    # reach into `presence._killed_cache` / `_killed_cache_at` / `_killed_lock`
    # — we expose these as @property so they transparently proxy to the
    # new halt-named attributes. Removed in v0.7.

    @property
    def _killed_cache(self) -> dict[str, dict[str, Any]]:
        """Deprecated alias of ``_halted_cache``. Removed in v0.7.

        Returns a view mapped to the v0.5.x key names so legacy tests that
        assert on ``entry["killed_by"]`` keep working.
        """
        return {
            aid: {
                "killed_by": entry.get("halted_by"),
                "killed_at": entry.get("halted_at"),
                "reason": entry.get("reason"),
            }
            for aid, entry in self._halted_cache.items()
        }

    @_killed_cache.setter
    def _killed_cache(self, value: dict[str, dict[str, Any]]) -> None:
        # v0.5.x tests assign dicts keyed by `killed_by`. Translate them
        # into the v0.6 halt shape on write.
        translated: dict[str, dict[str, Any]] = {}
        for aid, entry in (value or {}).items():
            translated[aid] = {
                "halted_by": entry.get("halted_by", entry.get("killed_by")),
                "halted_at": entry.get("halted_at", entry.get("killed_at")),
                "reason": entry.get("reason"),
            }
        self._halted_cache = translated

    @property
    def _killed_cache_at(self) -> float:
        """Deprecated alias of ``_halted_cache_at``. Removed in v0.7."""
        return self._halted_cache_at

    @_killed_cache_at.setter
    def _killed_cache_at(self, value: float) -> None:
        self._halted_cache_at = value

    @property
    def _killed_lock(self) -> asyncio.Lock:
        """Deprecated alias of ``_halted_lock``. Removed in v0.7."""
        return self._halted_lock

    async def close(self) -> None:
        """Release resources. Disposes the engine only if this module owns it."""
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()
            self._engine = None
