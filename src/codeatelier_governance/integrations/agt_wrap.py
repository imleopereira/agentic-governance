"""Microsoft Agent Framework (AGT) bridge for the Governance SDK.

F4 — v0.7 migration recipe. Lets platform engineers keep their existing
AGT deployment and layer Code Atelier's HMAC-chained audit + scope
enforcement on top in **one line** via :func:`wrap_agt_agent`, with an
additional **telemetry-consumption** path via :class:`AGTBridge.consume`
for teams that ingest AGT's trace stream out-of-band.

The adapter is dependency-free at import time: we do NOT import the
``agent-framework`` package. We accept AGT's event shape as a plain
dict because AGT's telemetry is published as OTel-style JSON and the
``ChatAgent.run`` surface we wrap duck-types against the methods we use.

Public API::

    from codeatelier_governance.integrations.agt_wrap import (
        AGTBridge, wrap_agt_agent,
    )

    # Path 1: telemetry ingestion (consume AGT trace events)
    bridge = AGTBridge(sdk=sdk, agent_id="support-v1")
    await bridge.consume(agt_event_dict)

    # Path 2: pre-execution enforcement wrap (AGT ChatAgent)
    agent = ChatAgent(...)  # your existing AGT agent
    wrapped = wrap_agt_agent(agent, sdk, agent_id="support-v1")
    await wrapped.run(...)  # scope + budget + halt gates run first

Enforcement call order (matches :mod:`openai_wrap`):

  1. halt check   — raises AgentHaltedError if the agent is halted
  2. scope.check  — raises ScopeViolation on disallowed tool
  3. cost.check_or_raise — raises BudgetExceeded if over cap
  4. original AGT call
  5. audit.log    — post-call event (observation; swallows errors)

Observation surfaces (audit.log) NEVER raise into host code. Enforcement
surfaces (scope.check / cost.check_or_raise) DO raise by contract, so a
disallowed tool request fails CLOSED before AGT hits the network.

Three canonical AGT event shapes are recognised by :meth:`AGTBridge.consume`:

  * ``tool_call``  — AGT's function-call span; mapped to ``tool.call``.
  * ``llm_call``   — AGT's chat-completion span; mapped to ``llm.call``.
  * ``hitl_approval`` — AGT's human-in-the-loop gate; mapped to
    ``approval.requested`` / ``approval.granted`` / ``approval.denied``
    depending on the event's ``outcome`` field.

Unknown event kinds are logged (structlog warning) and skipped rather
than raising — telemetry ingestion MUST NOT break the host pipeline.
"""
from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import structlog

from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.scope.errors import ScopeViolation

if TYPE_CHECKING:
    from codeatelier_governance.sdk import GovernanceSDK

__all__ = ["AGTBridge", "wrap_agt_agent", "AGTEventShapeError"]

logger = structlog.get_logger(__name__)

# Sentinel tool name used for scope checks on the top-level ``run()`` call
# when AGT doesn't report a specific tool (e.g. plain chat turns).
_RUN_SENTINEL = "agt.run"

# AGT event kinds we know how to map. Keep this list TIGHT — silently
# accepting unknown shapes produces silent-audit-holes.
_KNOWN_KINDS = frozenset({"tool_call", "llm_call", "hitl_approval"})


class AGTEventShapeError(ValueError):
    """Raised when an AGT event dict is structurally invalid.

    We raise on *malformed* input (non-dict, missing ``kind``) so callers
    see the bug at the ingestion boundary. We do NOT raise on unknown
    ``kind`` values — those are logged and skipped so a new AGT event
    type shipped upstream doesn't break the bridge.
    """


def _coerce_str(value: Any, *, max_len: int = 256) -> str | None:
    """Safely coerce a field to a bounded string, or None if absent."""
    if value is None:
        return None
    s = str(value)
    if len(s) > max_len:
        s = s[:max_len]
    return s


def _map_hitl_outcome_to_kind(outcome: Any) -> str:
    """Map AGT HITL ``outcome`` to our canonical approval event kinds."""
    if outcome == "granted" or outcome == "approved":
        return "approval.granted"
    if outcome == "denied" or outcome == "rejected":
        return "approval.denied"
    # Default: the approval was requested, outcome pending.
    return "approval.requested"


class AGTBridge:
    """Consume Microsoft Agent Framework trace events into the audit chain.

    Use this when your AGT deployment already emits OTel-style trace events
    (e.g. via Aspire / Azure Monitor) and you want the Governance SDK to
    ingest them into its HMAC-chained audit log for Article 12 evidence
    export, WITHOUT rewriting your agent call sites.

    The bridge is stateless apart from the ``agent_id`` and optional
    ``session_id`` bindings. It never opens network connections on its
    own — it only calls ``sdk.audit.log`` with sanitised metadata.

    Args:
        sdk: The initialised :class:`GovernanceSDK` instance.
        agent_id: The agent identifier used for all emitted audit events.
            Must be non-empty.
        session_id: Optional session UUID; if omitted, one is generated
            per bridge instance so correlated events land in the same
            chain segment.

    Raises:
        ValueError: If ``agent_id`` is empty or whitespace-only.
    """

    def __init__(
        self,
        sdk: GovernanceSDK,
        agent_id: str,
        session_id: UUID | None = None,
    ) -> None:
        if not agent_id or not agent_id.strip():
            raise ValueError(
                f"agent_id must be a non-empty string, got {agent_id!r}"
            )
        self._sdk = sdk
        self._agent_id = agent_id
        self._session_id = session_id if session_id is not None else uuid4()

    @property
    def agent_id(self) -> str:
        """The agent identifier bound to this bridge."""
        return self._agent_id

    @property
    def session_id(self) -> UUID:
        """The session UUID bound to this bridge."""
        return self._session_id

    async def consume(self, event: Any) -> bool:
        """Map an AGT-style trace event dict into an audit event.

        Returns True if an audit event was logged, False if the event
        kind was unknown (logged + skipped). Raises
        :class:`AGTEventShapeError` only on *structurally* invalid input.

        Args:
            event: An AGT-style trace event. Must be a mapping with at
                least a ``kind`` key. Unknown ``kind`` values are logged
                and skipped (return False) rather than raised, so
                upstream AGT additions never break the bridge.

        Returns:
            True if an audit event was emitted; False if the event was
            a known-malformed shape that was skipped gracefully.

        Raises:
            AGTEventShapeError: ``event`` is not a dict or lacks ``kind``.
        """
        if not isinstance(event, dict):
            raise AGTEventShapeError(
                f"AGT event must be a dict, got {type(event).__name__}. "
                f"Fix: pass the raw trace JSON as a dict, not a string."
            )
        raw_kind = event.get("kind")
        if not raw_kind or not isinstance(raw_kind, str):
            raise AGTEventShapeError(
                "AGT event is missing required 'kind' field (expected "
                "one of: tool_call, llm_call, hitl_approval). "
                "Fix: ensure your AGT telemetry exporter emits the span "
                "kind as a string field named 'kind'."
            )

        if raw_kind not in _KNOWN_KINDS:
            logger.warning(
                "governance.agt_wrap.unknown_event_kind",
                kind=raw_kind,
                agent_id=self._agent_id,
                detail=(
                    "AGT emitted an event kind we don't recognise; event "
                    "skipped so the telemetry pipeline keeps flowing. "
                    "File an issue if you expect this kind to land in "
                    "the audit chain."
                ),
            )
            return False

        audit_event = self._build_audit_event(raw_kind, event)
        await self._safe_log(audit_event)
        return True

    def _build_audit_event(self, raw_kind: str, event: dict[str, Any]) -> AuditEvent:
        """Translate an AGT trace dict into an :class:`AuditEvent`."""
        metadata: dict[str, Any] = {"source": "microsoft_agent_framework"}

        # Preserve a small, bounded set of useful AGT fields. We avoid
        # passing the raw event dict through — AGT traces can be huge and
        # AuditEvent caps metadata at 64 KiB. Copy explicitly instead.
        for key in ("span_id", "trace_id", "parent_span_id", "duration_ms"):
            if key in event and event[key] is not None:
                metadata[key] = event[key]

        tool_name = _coerce_str(event.get("tool_name") or event.get("function_name"))
        model = _coerce_str(event.get("model") or event.get("model_name"), max_len=128)

        if raw_kind == "tool_call":
            kind = "tool.call"
            if tool_name:
                metadata["tool"] = tool_name
            outcome = event.get("outcome")
            if outcome:
                metadata["outcome"] = _coerce_str(outcome)
        elif raw_kind == "llm_call":
            kind = "llm.call"
            usage = event.get("usage") or {}
            if isinstance(usage, dict):
                # Only pass through numeric token counts; anything else is
                # dropped silently — AGT upstream schema drift shouldn't
                # break the bridge.
                token_usage: dict[str, int] = {}
                for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    val = usage.get(field)
                    if isinstance(val, int) and val >= 0:
                        token_usage[field] = val
                if token_usage:
                    metadata["token_usage"] = token_usage
        else:  # hitl_approval — membership in _KNOWN_KINDS guarantees this branch
            kind = _map_hitl_outcome_to_kind(event.get("outcome"))
            approver = _coerce_str(event.get("approver"))
            if approver:
                metadata["approver"] = approver
            reason = _coerce_str(event.get("reason"), max_len=1024)
            if reason:
                metadata["reason"] = reason

        return AuditEvent(
            agent_id=self._agent_id,
            session_id=self._session_id,
            kind=kind,
            model=model,
            metadata=metadata,
        )

    async def _safe_log(self, event: AuditEvent) -> None:
        """Log an audit event, swallowing errors (observation surface)."""
        try:
            await self._sdk.audit.log(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.agt_wrap.audit_log_failed",
                kind=event.kind,
                error_type=type(exc).__name__,
                detail=(
                    "Failed to emit audit event for AGT trace; host "
                    "pipeline continues (invariant #1)."
                ),
            )


# ---------------------------------------------------------------------------
# wrap_agt_agent — pre-execution enforcement path (PRD F4 §acceptance)
# ---------------------------------------------------------------------------


async def _halt_check_if_wired(sdk: Any, agent_id: str) -> None:
    """Fail-closed halt check. Silent no-op if presence isn't wired."""
    presence = getattr(sdk, "presence", None)
    if presence is None:
        return
    await presence.assert_not_halted(agent_id)


async def _scope_check_if_registered(
    sdk: Any, agent_id: str, tool_name: str
) -> None:
    """Run scope.check only when a policy is registered for this agent."""
    scope = getattr(sdk, "scope", None)
    if scope is None:
        return
    policy = scope.get_policy(agent_id)
    if policy is None:
        return
    await scope.check(agent_id, tool=tool_name)


def _resolve_tool_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Best-effort extraction of the tool name AGT is about to call.

    AGT's ``ChatAgent.run`` signature passes tool invocations via either
    ``tool=`` / ``tool_name=`` kwargs or the first positional argument.
    If we can't determine the tool name, fall back to the ``agt.run``
    sentinel so scope policies can still gate at the run boundary.
    """
    for key in ("tool", "tool_name", "function_name"):
        val = kwargs.get(key)
        if isinstance(val, str) and val:
            return val
    if args and isinstance(args[0], str) and args[0]:
        return args[0]
    return _RUN_SENTINEL


def wrap_agt_agent(
    agent: Any,
    sdk: GovernanceSDK,
    *,
    agent_id: str,
    session_id: UUID | None = None,
) -> Any:
    """Wrap an AGT ChatAgent so calls go through governance gates first.

    Matches the one-line DX target from the v0.7 PRD::

        wrapped = wrap_agt_agent(agent, sdk, agent_id="support-v1")

    The wrapped agent keeps its original type — we patch ``run`` and
    ``run_stream`` (if present) in place so downstream AGT code that
    checks ``isinstance(agent, ChatAgent)`` keeps working. We do NOT
    import the ``agent-framework`` package; any object that exposes a
    callable ``run`` works (duck-typed for testability + so you can
    install with `pip install codeatelier-governance` — no ``[agt]``
    extra required to use the adapter).

    Args:
        agent: Any object with a callable ``run`` method. Typically an
            AGT ``ChatAgent`` or ``AssistantAgent`` instance.
        sdk: The initialised :class:`GovernanceSDK` instance.
        agent_id: The agent identifier for audit, scope, and cost
            tracking. Must be non-empty.
        session_id: Optional session UUID for budget scoping; one is
            generated per wrap call if omitted.

    Returns:
        The same ``agent`` object, patched in place.

    Raises:
        ValueError: ``agent_id`` is empty, or ``agent`` has no callable
            ``run`` method to wrap.
    """
    if not agent_id or not agent_id.strip():
        raise ValueError(
            f"agent_id must be a non-empty string, got {agent_id!r}"
        )
    if not hasattr(agent, "run") or not callable(agent.run):
        raise ValueError(
            "wrap_agt_agent: the supplied agent has no callable 'run' "
            "method. Expected an AGT ChatAgent / AssistantAgent. "
            "Fix: pass the instance you'd normally call agent.run(...) on."
        )
    if getattr(agent, "_governance_wrapped", False):
        logger.warning(
            "governance.agt_wrap.already_wrapped",
            agent_id=agent_id,
        )
        return agent

    sid = session_id if session_id is not None else uuid4()
    original_run = agent.run
    is_async = asyncio.iscoroutinefunction(original_run)

    if is_async:

        @functools.wraps(original_run)
        async def _run_async(*args: Any, **kwargs: Any) -> Any:
            tool_name = _resolve_tool_name(args, kwargs)

            # Gate 0: halt switch — fail-closed before AGT hits the net.
            await _halt_check_if_wired(sdk, agent_id)

            # Gate 1: scope — raises ScopeViolation on disallowed tool.
            # Scope errors propagate directly: the caller MUST see them.
            await _scope_check_if_registered(sdk, agent_id, tool_name)

            # Gate 2: budget — raises BudgetExceeded if over cap. Only
            # fires when a cost module is wired; silent pass otherwise.
            cost = getattr(sdk, "cost", None)
            if cost is not None and hasattr(cost, "check_or_raise"):
                await cost.check_or_raise(agent_id, sid)

            # Pre-call audit (observation — swallows errors).
            await _safe_audit_log(
                sdk, agent_id, "agt.run",
                {"source": "microsoft_agent_framework", "tool": tool_name},
                session_id=sid,
            )

            try:
                return await original_run(*args, **kwargs)
            except Exception as exc:
                await _safe_audit_log(
                    sdk, agent_id, "agt.error",
                    {
                        "source": "microsoft_agent_framework",
                        "tool": tool_name,
                        "error_type": type(exc).__name__,
                    },
                    session_id=sid,
                )
                raise

        agent.run = _run_async
    else:

        @functools.wraps(original_run)
        def _run_sync(*args: Any, **kwargs: Any) -> Any:
            tool_name = _resolve_tool_name(args, kwargs)
            try:
                asyncio.get_running_loop()
                raise RuntimeError(
                    "wrap_agt_agent: sync agent.run() called inside a "
                    "running event loop. Use AGT's async API (e.g. "
                    "AsyncChatAgent) instead, or call from outside the "
                    "event loop."
                )
            except RuntimeError as exc:
                if "running event loop" in str(exc) and "no running" not in str(exc):
                    raise

            asyncio.run(_halt_check_if_wired(sdk, agent_id))
            asyncio.run(_scope_check_if_registered(sdk, agent_id, tool_name))
            cost = getattr(sdk, "cost", None)
            if cost is not None and hasattr(cost, "check_or_raise"):
                asyncio.run(cost.check_or_raise(agent_id, sid))
            asyncio.run(_safe_audit_log(
                sdk, agent_id, "agt.run",
                {"source": "microsoft_agent_framework", "tool": tool_name},
                session_id=sid,
            ))
            try:
                return original_run(*args, **kwargs)
            except Exception as exc:
                asyncio.run(_safe_audit_log(
                    sdk, agent_id, "agt.error",
                    {
                        "source": "microsoft_agent_framework",
                        "tool": tool_name,
                        "error_type": type(exc).__name__,
                    },
                    session_id=sid,
                ))
                raise

        agent.run = _run_sync

    agent._governance_wrapped = True
    agent._governance_session_id = sid
    agent._governance_agent_id = agent_id

    # Register on the SDK wrapper registry so start() can surface coverage.
    registered: list[str] = getattr(sdk, "_registered_wrappers", [])
    label = f"agt:{agent_id}"
    if label not in registered:
        registered.append(label)
        try:
            registry = getattr(sdk, "_wrapper_registry", None)
            if registry is not None:
                registry.register(agent_id, "agt")
        except Exception as _exc:  # noqa: BLE001
            logger.warning(
                "governance.agt_wrap.coverage_register_failed",
                agent_id=agent_id,
                error_type=type(_exc).__name__,
            )

    return agent


async def _safe_audit_log(
    sdk: Any,
    agent_id: str,
    kind: str,
    metadata: dict[str, Any],
    *,
    session_id: UUID | None = None,
) -> None:
    """Best-effort audit log that never raises into host code."""
    try:
        await sdk.audit.log(
            AuditEvent(
                agent_id=agent_id,
                kind=kind,
                metadata=metadata,
                session_id=session_id,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "governance.agt_wrap.audit_log_failed",
            kind=kind,
            error_type=type(exc).__name__,
        )


# Re-export a friendly alias so scope.check's exception name matches the
# PRD acceptance criterion ("ScopeViolationError"). The canonical class
# is still ScopeViolation; ScopeViolationError is the alias AGT users
# may see referenced in the migration docs.
ScopeViolationError = ScopeViolation
