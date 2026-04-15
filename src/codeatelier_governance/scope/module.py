"""Scope enforcement module exposed via ``sdk.scope``.

Public API:
    sdk.scope.register(policy)              # called once at SDK init
    await sdk.scope.check(agent_id, tool="...")
    await sdk.scope.check(agent_id, api="POST https://api.x.com/...")
    @sdk.scope.require_tool("send_email", agent_id="x")

Every violation is auto-logged as an audit event with kind="scope.violation".
Default deny: an agent with no registered policy fails every check.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar

import structlog

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from .errors import PolicyNotRegistered, ScopeViolation
from .models import ScopePolicy

P = ParamSpec("P")
R = TypeVar("R")

logger = structlog.get_logger(__name__)


class ScopeModule:
    """Action scope enforcement.

    Policies are registered at SDK init time. The agent's execution context
    cannot register or modify policies — there is no public mutation API
    other than ``register``, which is intended for application startup.
    """

    def __init__(
        self,
        audit: AuditModule,
        policies: list[ScopePolicy] | None = None,
        *,
        database_url: str | None = None,
        engine: Any = None,
    ) -> None:
        self._audit = audit
        self._policies: dict[str, ScopePolicy] = {}
        self._database_url = database_url
        self._engine: Any = engine
        self._owns_engine = False
        # Strong references to in-flight DB upsert tasks so they are not
        # GC'd mid-execution.  Populated when register() is called with a
        # running event loop.
        self._pending_upsert_tasks: set[asyncio.Task[Any]] = set()
        # Policies registered before any event loop was running.  Drained
        # by ``flush_pending_upserts()`` at SDK start().  This replaces
        # the v0.5.0 behaviour of calling ``asyncio.run()`` inline, which
        # violated invariant #3 ("SDK MUST NOT hold long-running
        # connections in the user's request path") during sync startup.
        self._pending_upsert_policies: list[ScopePolicy] = []
        # Optional reference to PresenceModule for the v0.5.4 kill switch.
        # Wired by GovernanceSDK after construction via set_presence_module().
        # If None, scope.check() runs without a kill check (degrades to v0.5.3
        # behaviour). This is a deliberate optional dependency: ScopeModule
        # can still be constructed and tested in isolation.
        self._presence: Any = None
        for policy in policies or []:
            self._policies[policy.agent_id] = policy

    def set_presence_module(self, presence: Any) -> None:
        """Wire the PresenceModule for kill-switch enforcement (v0.5.4).

        Called by GovernanceSDK during start() after both modules exist.
        Once set, every scope.check() call will first call
        presence.assert_alive(agent_id) and fail-closed with AgentKilledError
        if the agent has been killed by an operator via the console.
        """
        self._presence = presence

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

    def register(self, policy: ScopePolicy) -> None:
        """Register a policy for an agent. Call at app startup, not at runtime."""
        self._policies[policy.agent_id] = policy
        self._persist_policy_best_effort(policy.agent_id, "scope", policy)

    def _persist_policy_best_effort(
        self, agent_id: str, policy_type: str, policy: ScopePolicy,
    ) -> None:
        """Best-effort upsert of a policy to Postgres. Never raises.

        Behaviour:
          * If a running event loop is available, schedules the upsert as
            a background task held in ``_pending_upsert_tasks`` (no
            fire-and-forget, no GC loss).
          * If NO event loop is running (the sync-startup path), queues
            the policy in ``_pending_upsert_policies`` to be drained by
            :meth:`flush_pending_upserts` at ``sdk.start()``.  This avoids
            the v0.5.0 anti-pattern of calling ``asyncio.run()`` inline,
            which violated invariant #3 and could deadlock in sync
            codebases that already owned an outer loop.
        """
        engine = self._get_engine()
        if engine is None:
            return
        try:
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop is not None and loop.is_running():
                task = loop.create_task(
                    self._upsert_policy(engine, agent_id, policy_type, policy)
                )
                self._pending_upsert_tasks.add(task)
                task.add_done_callback(
                    self._pending_upsert_tasks.discard
                )
            else:
                # Defer until sdk.start() drains the queue.
                self._pending_upsert_policies.append(policy)
                logger.info(
                    "scope.persist_policy_deferred",
                    agent_id=agent_id,
                    policy_type=policy_type,
                    detail=(
                        "No event loop running at register() time; "
                        "upsert deferred until sdk.start()."
                    ),
                )
        except Exception as exc:
            logger.warning(
                "scope.persist_policy_failed",
                agent_id=agent_id,
                policy_type=policy_type,
                exc_type=type(exc).__name__,
            )

    async def flush_pending_upserts(self) -> None:
        """Drain policies that were registered before the event loop started.

        Called from ``GovernanceSDK.start()``.  Never raises — failures
        are logged and the queue is cleared so retries do not pile up.
        """
        if not self._pending_upsert_policies:
            return
        engine = self._get_engine()
        if engine is None:
            self._pending_upsert_policies.clear()
            return
        pending = self._pending_upsert_policies
        self._pending_upsert_policies = []
        for policy in pending:
            try:
                await self._upsert_policy(
                    engine, policy.agent_id, "scope", policy
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "scope.deferred_upsert_failed",
                    agent_id=policy.agent_id,
                    exc_type=type(exc).__name__,
                )

    @staticmethod
    async def _upsert_policy(
        engine: Any,
        agent_id: str,
        policy_type: str,
        policy: ScopePolicy,
    ) -> None:
        """Upsert a policy row into governance_policies."""
        from sqlalchemy import text

        policy_json = json.dumps(policy.model_dump(mode="json"))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_policies (agent_id, policy_type, policy_json, updated_at) "
                    "VALUES (:agent_id, :policy_type, CAST(:policy_json AS jsonb), NOW()) "
                    "ON CONFLICT (agent_id, policy_type) "
                    "DO UPDATE SET policy_json = CAST(:policy_json AS jsonb), updated_at = NOW()"
                ),
                {
                    "agent_id": agent_id,
                    "policy_type": policy_type,
                    "policy_json": policy_json,
                },
            )

    async def get_stored_policies(self) -> list[ScopePolicy]:
        """Read scope policies from Postgres. Returns an empty list if no DB."""
        engine = self._get_engine()
        if engine is None:
            return []
        from sqlalchemy import text

        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT policy_json FROM governance_policies "
                    "WHERE policy_type = :policy_type "
                    "ORDER BY agent_id"
                ),
                {"policy_type": "scope"},
            )
            rows = list(res.mappings())
        policies: list[ScopePolicy] = []
        for row in rows:
            data = row["policy_json"]
            if isinstance(data, str):
                data = json.loads(data)
            policies.append(ScopePolicy.model_validate(data))
        return policies

    def get_policy(self, agent_id: str) -> ScopePolicy | None:
        return self._policies.get(agent_id)

    def filter_tools(self, agent_id: str, tools: list[str]) -> list[str]:
        """Remove hidden tools from a tool list.

        Used before passing tools to the LLM so the agent never even
        knows the hidden tools exist.

        **Fail-closed on unknown agent.** If no policy is registered for
        ``agent_id``, this method raises :class:`PolicyNotRegistered` to
        mirror :meth:`check`'s default-deny contract.  Prior to v0.5.1 the
        method returned the full tool list unchanged when no policy was
        registered — an unintentional bypass that let ``hidden_tools``
        leak to unregistered agents.  Callers that previously relied on
        the pass-through behaviour must now either register a policy
        (possibly with ``allowed_tools=frozenset()`` for a no-op policy)
        or catch ``PolicyNotRegistered`` at the call site and decide
        explicitly whether to pass or drop the tool list.

        Raises:
            PolicyNotRegistered: no policy exists for ``agent_id``.
        """
        policy = self._policies.get(agent_id)
        if policy is None:
            raise PolicyNotRegistered(
                f"scope.filter_tools: no policy registered for agent_id="
                f"{agent_id!r}.  Fail-closed as of v0.5.1 — previous "
                f"versions silently returned the full tool list, which "
                f"bypassed hidden_tools for unregistered agents.\n"
                f"Fix: call sdk.scope.register(ScopePolicy(agent_id=..., "
                f"allowed_tools=..., hidden_tools=...)) at startup, or "
                f"catch PolicyNotRegistered at the call site and decide "
                f"explicitly whether to drop the tool list.",
                recovery_hint=(
                    "Register a ScopePolicy for this agent_id OR catch "
                    "PolicyNotRegistered and handle it explicitly."
                ),
            )
        if not policy.hidden_tools:
            return tools
        return [t for t in tools if t not in policy.hidden_tools]

    async def check(
        self,
        agent_id: str,
        *,
        tool: str | None = None,
        api: str | None = None,
    ) -> None:
        """Check whether ``agent_id`` is permitted to call ``tool`` or ``api``.

        Raises:
            AgentKilledError: an operator has killed this agent via the
                console kill switch (v0.5.4 hotfix). Fail-closed before
                any other check. The kill check is fast (5-second TTL cache,
                no DB query on the hot path) and degrades gracefully if the
                governance DB is unreachable (Invariant #1).
            PolicyNotRegistered: no policy exists for ``agent_id``.
            ScopeViolation: the action is outside the registered scope.
        """
        # v0.5.4 kill switch — first thing in the check.
        # If presence module is wired, fail-closed on killed agents BEFORE
        # any policy lookup. Skipped silently if no presence module is
        # configured (back-compat with v0.5.3 SDK construction).
        if self._presence is not None:
            await self._presence.assert_alive(agent_id)

        if tool is None and api is None:
            raise ValueError(
                "scope.check: pass either tool=... or api=... (or both). "
                "Fix: sdk.scope.check(agent_id, tool='read_invoice')"
            )
        policy = self._policies.get(agent_id)
        if policy is None:
            await self._log_violation(
                agent_id, tool, api, reason="no policy registered"
            )
            raise PolicyNotRegistered(
                f"scope check failed: no policy registered for agent_id={agent_id!r}.\n"
                f"Fix: call sdk.scope.register(ScopePolicy(agent_id=..., allowed_tools=...)) at startup.",
                recovery_hint="Register a ScopePolicy for this agent_id before calling check().",
            )
        if tool is not None and tool not in policy.allowed_tools:
            await self._log_violation(
                agent_id, tool, api, reason="tool not whitelisted"
            )
            allowed_preview = sorted(policy.allowed_tools)[:10]
            suffix = f" ... and {len(policy.allowed_tools) - 10} more" if len(policy.allowed_tools) > 10 else ""
            raise ScopeViolation(
                f"Scope violation: tool {tool!r} is not allowed for agent {agent_id!r}.\n"
                f"Allowed tools: [{', '.join(allowed_preview)}{suffix}]\n"
                f"Fix: add {tool!r} to ScopePolicy.allowed_tools for this agent.",
                recovery_hint=f"Add {tool!r} to ScopePolicy(allowed_tools=frozenset({{...}})) at startup.",
            )
        if api is not None and not _api_matches(api, policy.allowed_apis):
            await self._log_violation(
                agent_id, tool, api, reason="api not whitelisted"
            )
            allowed_preview = sorted(policy.allowed_apis)[:10]
            suffix = f" ... and {len(policy.allowed_apis) - 10} more" if len(policy.allowed_apis) > 10 else ""
            raise ScopeViolation(
                f"Scope violation: api {api!r} is not allowed for agent {agent_id!r}.\n"
                f"Allowed APIs: [{', '.join(allowed_preview)}{suffix}]\n"
                f"Fix: add {api!r} to ScopePolicy.allowed_apis for this agent.",
                recovery_hint=f"Add {api!r} to ScopePolicy(allowed_apis=frozenset({{...}})) at startup.",
            )

    async def _log_violation(
        self,
        agent_id: str,
        tool: str | None,
        api: str | None,
        reason: str,
    ) -> None:
        await self._audit.log(
            AuditEvent(
                agent_id=agent_id,
                kind="scope.violation",
                metadata={
                    "tool": tool,
                    "api": api,
                    "reason": reason,
                },
            )
        )

    def require_tool(
        self,
        tool: str,
        *,
        agent_id: str,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """Decorator that enforces ``tool`` is in ``agent_id``'s scope before call."""

        def decorator(
            func: Callable[P, Awaitable[R]],
        ) -> Callable[P, Awaitable[R]]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"@scope.require_tool requires an async function; "
                    f"{func.__name__} is sync. "
                    f"Fix: convert to `async def {func.__name__}(...)`."
                )

            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                await self.check(agent_id=agent_id, tool=tool)
                return await func(*args, **kwargs)

            return wrapper

        return decorator


def _api_matches(api: str, patterns: frozenset[str]) -> bool:
    """Exact match OR explicit prefix match.

    A pattern ending in '*' matches any api starting with the prefix BEFORE
    the asterisk. Glob/regex are intentionally not supported — confusion or
    injection in the pattern syntax becomes an authorization bypass.
    """
    for pattern in patterns:
        if pattern.endswith("*"):
            if api.startswith(pattern[:-1]):
                return True
        elif api == pattern:
            return True
    return False
