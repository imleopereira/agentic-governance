"""Behavioral contracts module exposed via ``sdk.contracts``.

Public API:
    sdk.contracts.register(contract)
    await sdk.contracts.check_pre(agent_id, session_id, tool)
    await sdk.contracts.check_post(agent_id, session_id, tool)
    async with sdk.contracts.enforce(agent_id, session_id, tool): ...
    sdk.contracts.register_check(name, fn)

Pre/post conditions on tool calls, enforced at runtime. This unifies
scope+cost+HITL into one declarative surface.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Awaitable
from uuid import UUID

import structlog

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from ..cost.errors import BudgetExceeded
from ..cost.module import CostModule
from ..gates.store import GatesStore
from ..scope.errors import ScopeViolation, PolicyNotRegistered
from ..scope.module import ScopeModule
from .errors import ContractViolation
from .models import Contract, PreCondition, PostCondition

logger = structlog.get_logger(__name__)


class ContractsModule:
    """Agent behavioral contracts — pre/post condition enforcement.

    Contracts are registered at SDK init time. Each contract binds to an
    ``(agent_id, tool)`` pair and declares pre-conditions (checked before
    the tool call) and post-conditions (checked after).

    Built-in checks:
        Pre:  ``hitl_approved``, ``budget_available``, ``scope_allowed``, ``custom``
        Post: ``audit_logged``, ``custom``
    """

    def __init__(
        self,
        audit: AuditModule,
        scope: ScopeModule,
        cost: CostModule,
        gates: Any | None = None,
    ) -> None:
        self._audit = audit
        self._scope = scope
        self._cost = cost
        self._gates = gates
        self._contracts: dict[tuple[str, str], Contract] = {}
        self._custom_checks: dict[str, Callable[..., Awaitable[bool]]] = {}
        self._lock = asyncio.Lock()

    def register(self, contract: Contract) -> None:
        """Register a contract for an (agent_id, tool) pair. Call at app startup."""
        self._contracts[(contract.agent_id, contract.tool)] = contract

    def register_check(
        self,
        name: str,
        fn: Callable[..., Awaitable[bool]],
    ) -> None:
        """Register a custom async callable for use in ``custom`` checks.

        The callable receives ``(agent_id, session_id, tool)`` and must
        return ``True`` (pass) or ``False`` (fail).
        """
        if not name or len(name) > 256:
            raise ValueError(
                f"custom check name must be 1-256 chars, got {len(name)!r}"
            )
        self._custom_checks[name] = fn

    def get_contract(self, agent_id: str, tool: str) -> Contract | None:
        """Return the contract for (agent_id, tool), or None."""
        return self._contracts.get((agent_id, tool))

    def list_contracts(self, agent_id: str) -> list[Contract]:
        """Return all contracts registered for a given agent."""
        return [
            c for (aid, _), c in self._contracts.items() if aid == agent_id
        ]

    async def check_pre(
        self,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> None:
        """Evaluate all pre-conditions for this (agent_id, tool).

        If ANY pre-condition fails, emits a ``contract.pre_violation`` audit
        event and raises :class:`ContractViolation`.

        No-op if no contract is registered for this (agent_id, tool).
        """
        contract = self._contracts.get((agent_id, tool))
        if contract is None:
            return

        for condition in contract.pre:
            passed = await self._eval_pre(condition, agent_id, session_id, tool)
            if not passed:
                await self._audit.log(
                    AuditEvent(
                        agent_id=agent_id,
                        session_id=session_id,
                        kind="contract.pre_violation",
                        metadata={
                            "tool": tool,
                            "check": condition.check,
                            "message": condition.message,
                        },
                    )
                )
                raise ContractViolation(
                    f"contract pre-condition failed: tool={tool!r}, "
                    f"check={condition.check!r}, message={condition.message!r}"
                )

    async def check_post(
        self,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> None:
        """Evaluate all post-conditions for this (agent_id, tool).

        If ANY post-condition fails, emits a ``contract.post_violation`` audit
        event and raises :class:`ContractViolation`.

        No-op if no contract is registered for this (agent_id, tool).
        """
        contract = self._contracts.get((agent_id, tool))
        if contract is None:
            return

        for condition in contract.post:
            passed = await self._eval_post(condition, agent_id, session_id, tool)
            if not passed:
                await self._audit.log(
                    AuditEvent(
                        agent_id=agent_id,
                        session_id=session_id,
                        kind="contract.post_violation",
                        metadata={
                            "tool": tool,
                            "check": condition.check,
                            "message": condition.message,
                        },
                    )
                )
                raise ContractViolation(
                    f"contract post-condition failed: tool={tool!r}, "
                    f"check={condition.check!r}, message={condition.message!r}"
                )

    @asynccontextmanager
    async def enforce(
        self,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> AsyncIterator[None]:
        """Context manager: check_pre, yield, check_post.

        Usage:
            async with sdk.contracts.enforce(agent_id, session_id, "charge_customer"):
                # ... do the tool call ...
        """
        await self.check_pre(agent_id, session_id, tool)
        yield
        await self.check_post(agent_id, session_id, tool)

    # ------------------------------------------------------------------
    # Built-in pre-condition evaluators
    # ------------------------------------------------------------------

    async def _eval_pre(
        self,
        condition: PreCondition,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> bool:
        """Evaluate a single pre-condition. Returns True if passed."""
        if condition.check == "hitl_approved":
            return await self._check_hitl_approved(agent_id, session_id)
        if condition.check == "budget_available":
            return await self._check_budget_available(agent_id, session_id)
        if condition.check == "scope_allowed":
            return await self._check_scope_allowed(agent_id, tool)
        if condition.check == "custom":
            return await self._check_custom(condition, agent_id, session_id, tool)
        return False  # Unknown check = fail closed

    async def _check_hitl_approved(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> bool:
        """Check if there is an unexpired granted HITL approval for ``agent_id``.

        Delegates to :meth:`GatesStore.has_granted_approval`, which is
        implemented by both ``InMemoryGatesStore`` and
        ``PostgresGatesStore``.  Fixed in v0.5.1 — prior versions reached
        into in-memory store private attributes and returned ``False``
        unconditionally for Postgres, which silently broke every
        HITL-gated contract deployed with a Postgres backend (legitimate
        approved actions were blocked — over-blocking, not bypass).
        """
        if self._gates is None:
            logger.warning(
                "contracts.hitl_check_no_gates_module",
                agent_id=agent_id,
            )
            return False
        try:
            # Annotate locally so mypy resolves has_granted_approval to
            # GatesStore's bool-typed method.  self._gates itself is
            # ``Any | None`` (legacy), so without this hint the call
            # would return Any and trip --strict's no-any-return rule.
            store: GatesStore = self._gates._store
            return await store.has_granted_approval(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "contracts.hitl_check_failed",
                agent_id=agent_id,
                exc_type=type(exc).__name__,
            )
            return False

    async def _check_budget_available(
        self,
        agent_id: str,
        session_id: UUID,
    ) -> bool:
        """Check if the budget has not been exceeded."""
        try:
            await self._cost.check_or_raise(agent_id, session_id)
            return True
        except BudgetExceeded:
            return False

    async def _check_scope_allowed(
        self,
        agent_id: str,
        tool: str,
    ) -> bool:
        """Check if the tool is in scope for this agent."""
        try:
            await self._scope.check(agent_id, tool=tool)
            return True
        except (ScopeViolation, PolicyNotRegistered):
            return False

    # ------------------------------------------------------------------
    # Built-in post-condition evaluators
    # ------------------------------------------------------------------

    async def _eval_post(
        self,
        condition: PostCondition,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> bool:
        """Evaluate a single post-condition. Returns True if passed."""
        if condition.check == "audit_logged":
            return await self._check_audit_logged(agent_id, session_id, tool)
        if condition.check == "custom":
            return await self._check_custom(condition, agent_id, session_id, tool)
        return False  # Unknown check = fail closed

    async def _check_audit_logged(
        self,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> bool:
        """Check if there is an audit event for this (agent_id, session_id, tool) recently."""
        try:
            store = self._audit._store
            events = await store.get_session_events(session_id)
            # Look for any event referencing this tool in metadata within the session
            for event in events:
                meta = event.metadata
                if meta.get("tool") == tool:
                    return True
            return False
        except Exception as exc:
            logger.warning(
                "contracts.audit_logged_check_failed",
                agent_id=agent_id,
                session_id=str(session_id),
                exc_type=type(exc).__name__,
            )
            return False

    # ------------------------------------------------------------------
    # Custom check evaluator
    # ------------------------------------------------------------------

    async def _check_custom(
        self,
        condition: PreCondition | PostCondition,
        agent_id: str,
        session_id: UUID,
        tool: str,
    ) -> bool:
        """Look up and execute a registered custom check."""
        callable_name = condition.params.get("callable")
        if callable_name is None:
            logger.warning(
                "contracts.custom_check_missing_callable",
                check=condition.check,
                message=condition.message,
            )
            return False
        fn = self._custom_checks.get(callable_name)
        if fn is None:
            logger.warning(
                "contracts.custom_check_not_registered",
                callable_name=callable_name,
            )
            return False
        try:
            result = await fn(agent_id, session_id, tool)
            return bool(result)
        except Exception as exc:
            logger.warning(
                "contracts.custom_check_failed",
                callable_name=callable_name,
                agent_id=agent_id,
                exc_type=type(exc).__name__,
            )
            return False
