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
    ) -> None:
        self._audit = audit
        self._policies: dict[str, ScopePolicy] = {}
        self._lock = asyncio.Lock()
        self._database_url = database_url
        self._engine: Any = None
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

    def register(self, policy: ScopePolicy) -> None:
        """Register a policy for an agent. Call at app startup, not at runtime."""
        self._policies[policy.agent_id] = policy
        self._persist_policy_best_effort(policy.agent_id, "scope", policy)

    def _persist_policy_best_effort(
        self, agent_id: str, policy_type: str, policy: ScopePolicy,
    ) -> None:
        """Best-effort upsert of a policy to Postgres. Never raises."""
        engine = self._get_engine()
        if engine is None:
            return
        try:
            import asyncio as _asyncio

            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = _asyncio.get_running_loop()
            except RuntimeError:
                pass

            if loop is not None and loop.is_running():
                loop.create_task(
                    self._upsert_policy(engine, agent_id, policy_type, policy)
                )
            else:
                _asyncio.run(
                    self._upsert_policy(engine, agent_id, policy_type, policy)
                )
        except Exception as exc:
            logger.warning(
                "scope.persist_policy_failed",
                agent_id=agent_id,
                policy_type=policy_type,
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
                    "VALUES (:agent_id, :policy_type, :policy_json::jsonb, NOW()) "
                    "ON CONFLICT (agent_id, policy_type) "
                    "DO UPDATE SET policy_json = :policy_json::jsonb, updated_at = NOW()"
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
        knows the hidden tools exist. If no policy is registered for
        the agent, the full list is returned unchanged.
        """
        policy = self._policies.get(agent_id)
        if policy is None or not policy.hidden_tools:
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
            PolicyNotRegistered: no policy exists for ``agent_id``.
            ScopeViolation: the action is outside the registered scope.
        """
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
