"""Anthropic SDK wrapper that patches a client to emit governance events.

Usage::

    import anthropic
    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.integrations.anthropic_wrap import wrap_anthropic

    sdk = GovernanceSDK(database_url="postgresql://...")
    client = wrap_anthropic(anthropic.Anthropic(), sdk=sdk, agent_id="my-agent")
    # client.messages.create() now emits audit events + tracks cost

The wrapper monkey-patches the client in-place and returns it so existing
references keep working.

Observation surfaces (audit.log, cost.track) never raise. The enforcement
surfaces (scope.check, cost.check_or_raise) DO raise by contract.

Enforcement call order (both async and sync paths):
  1. scope.check  — raises ScopeViolation if denied (skipped when no policy registered)
  2. cost.check_or_raise — raises BudgetExceeded if over budget
  3. LLM call
  4. cost.track — post-call usage recording
  5. audit.log  — post-call event logging
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any
from uuid import UUID, uuid4

import structlog

from codeatelier_governance.audit.models import AuditEvent

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeatelier_governance.sdk import GovernanceSDK

logger = structlog.get_logger(__name__)

# Sentinel tool name used for scope checks at the LLM-call interception point.
# Anthropic calls have no explicit tool name here, so we use a stable sentinel.
_MESSAGES_CREATE_SENTINEL = "messages.create"


async def _scope_check_if_registered(sdk: Any, agent_id: str, tool_name: str) -> None:
    """Run scope.check() only when a policy is registered for this agent.

    Fires BEFORE the cost check so a scope violation never reaches the cost gate.
    Silently passes through when no scope module is available or no policy is
    registered for ``agent_id`` — this preserves backward compatibility for
    existing users who have not yet configured scope policies.

    Args:
        sdk: The GovernanceSDK instance (typed as Any to avoid circular import).
        agent_id: The agent identifier to check.
        tool_name: The tool or action name to check against the policy.

    Raises:
        ScopeViolation: if the agent has a registered policy and ``tool_name``
            is not in its allowed_tools set.
        PolicyNotRegistered: if the agent has a registered policy but the
            check method raises PolicyNotRegistered (should not normally happen
            since we check is_registered first, but guard anyway).
    """
    scope = getattr(sdk, "scope", None)
    if scope is None:
        return
    policy = scope.get_policy(agent_id)
    if policy is None:
        # No scope policy registered for this agent — open scope, pass through.
        return
    await scope.check(agent_id, tool=tool_name)


def _extract_token_usage(response: Any) -> dict[str, int]:
    """Extract token usage from an Anthropic response object.

    Anthropic responses expose ``response.usage.input_tokens`` and
    ``response.usage.output_tokens``. There is no ``total_tokens``
    field -- we compute it as the sum.
    """
    usage: dict[str, int] = {}
    if hasattr(response, "usage") and response.usage is not None:
        u = response.usage
        if hasattr(u, "input_tokens"):
            usage["input_tokens"] = int(u.input_tokens)
        if hasattr(u, "output_tokens"):
            usage["output_tokens"] = int(u.output_tokens)
        if "input_tokens" in usage and "output_tokens" in usage:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _estimate_usd(model: str, usage: dict[str, int]) -> float:
    """Estimate USD cost using the built-in pricing table.

    Returns 0.0 if the model is unknown or usage is empty.
    """
    from codeatelier_governance.cost.pricing import estimate_cost

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    return estimate_cost(model, input_tokens, output_tokens)


def _has_running_loop() -> bool:
    """Check if there is a running event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _resolve_projected_tokens(sdk: Any, kwargs: dict[str, Any]) -> int | None:
    """Resolve the projected token count for the upcoming call.

    Resolution order:
    1. ``max_tokens`` from the call kwargs (caller declared it explicitly).
    2. ``sdk.config.default_max_tokens`` (SDK-level default).
    3. None — emit a structlog warning; the cost gate falls back to the
       backward-looking ``current_balance > limit`` check.

    Args:
        sdk: The GovernanceSDK instance.
        kwargs: The keyword arguments passed to messages.create / completions.create.

    Returns:
        The projected token count, or None if unavailable.
    """
    if "max_tokens" in kwargs:
        return int(kwargs["max_tokens"])
    default = getattr(getattr(sdk, "config", None), "default_max_tokens", None)
    if default is not None:
        return int(default)
    logger.warning(
        "governance.wrap.max_tokens_not_declared",
        detail=(
            "max_tokens was not passed to the LLM call and no default_max_tokens "
            "is configured on GovernanceSDK. Budget gate cannot project forward — "
            "falling back to balance-only check. To enable forward projection, "
            "pass max_tokens= in your API call or set "
            "GovernanceSDK(..., default_max_tokens=N)."
        ),
    )
    return None


async def _handle_streaming_response(
    sdk: Any,
    agent_id: str,
    session_id: UUID,
    stream: Any,
    model: str,
    projected_tokens: int | None,
) -> None:
    """Log the streaming audit event with cost_tracked=True.

    For async streaming responses we cannot iterate the stream inside the wrapper
    (the caller needs the iterable).  Instead we immediately use ``projected_tokens``
    (max_tokens from the call kwargs or SDK default) as the tracked usage and emit
    a ``llm.result`` event with ``cost_tracked=True``.

    If ``projected_tokens`` is unavailable, a warning is emitted and
    ``cost_tracked=False`` is recorded — the operator must call
    ``sdk.cost.track()`` manually.

    Args:
        sdk: The GovernanceSDK instance.
        agent_id: The agent identifier.
        session_id: The current session UUID.
        stream: The streaming response object (not consumed here).
        model: The model name used for this call.
        projected_tokens: The declared max_tokens or SDK default, if available.
    """
    if projected_tokens is not None and projected_tokens > 0:
        await _safe_cost_track(sdk, agent_id, session_id, projected_tokens, 0.0)
        await _safe_audit_log(
            sdk, agent_id, "llm.result",
            {"model": model, "streaming": True, "cost_tracked": True,
             "projected_tokens_tracked": projected_tokens},
            model=str(model), session_id=session_id,
        )
    else:
        logger.warning(
            "governance.anthropic_wrap.streaming_no_usage",
            agent_id=agent_id,
            detail=(
                "Streaming call with no max_tokens declared and no SDK default. "
                "Cost not tracked for this call. Pass max_tokens= in your API "
                "call or set GovernanceSDK(..., default_max_tokens=N)."
            ),
        )
        await _safe_audit_log(
            sdk, agent_id, "llm.result",
            {"model": model, "streaming": True, "cost_tracked": False},
            model=str(model), session_id=session_id,
        )


async def _safe_audit_log(
    sdk: GovernanceSDK,
    agent_id: str,
    kind: str,
    metadata: dict[str, Any],
    model: str | None = None,
    session_id: UUID | None = None,
) -> None:
    """Audit log that swallows all errors (observation surface)."""
    try:
        await sdk.audit.log(
            AuditEvent(
                agent_id=agent_id,
                kind=kind,
                metadata=metadata,
                model=model,
                session_id=session_id,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "governance.anthropic_wrap.audit_log_failed",
            kind=kind,
            error_type=type(exc).__name__,
        )


async def _safe_cost_track(sdk: GovernanceSDK, agent_id: str, session_id: UUID, tokens: int, usd: float) -> None:
    """Track cost, swallowing all errors (observation surface)."""
    try:
        await sdk.cost.track(agent_id, session_id, tokens=tokens, usd=usd)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "governance.anthropic_wrap.cost_track_failed",
            error_type=type(exc).__name__,
        )


def _is_streaming_response(response: Any) -> bool:
    """Detect Anthropic streaming response objects (MessageStream / AsyncStream)."""
    type_name = type(response).__name__
    return type_name in ("MessageStream", "AsyncMessageStream", "Stream", "AsyncStream")


def _wrap_sync_create(
    original: Any,
    sdk: GovernanceSDK,
    agent_id: str,
    session_id: UUID,
) -> Any:
    """Wrap a sync messages.create method.

    If called from within an existing async event loop (e.g. from a test
    or a framework that nests sync in async), the wrapper raises a clear
    error directing the developer to use the async API. When there is no
    running loop, it uses ``asyncio.run`` for each governance call.
    """

    async def _async_impl(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model", "unknown")
        is_streaming = kwargs.get("stream", False)

        # Model selection policy — advisory, never raises
        if getattr(sdk, "routing", None) is not None and sdk.routing.has_policies():
            _suggested = await sdk.routing.suggest(
                agent_id, session_id, str(model),
                max_tokens=int(kwargs.get("max_tokens", 1000)),
            )
            if _suggested != str(model):
                kwargs["model"] = _suggested
                model = _suggested

        # Enforcement gate 1: scope — raises ScopeViolation if denied.
        # Fires before cost so a scope denial never touches the budget counter.
        await _scope_check_if_registered(sdk, agent_id, _MESSAGES_CREATE_SENTINEL)

        # Enforcement gate 2: budget — raises BudgetExceeded if over cap.
        # Pass projected_tokens so the gate blocks calls that would exceed
        # the limit, not just calls that already exceeded it.
        _projected = _resolve_projected_tokens(sdk, kwargs)
        await sdk.cost.check_or_raise(agent_id, session_id, projected_tokens=_projected)

        # Background the pre-call audit (observation-only, not enforcement)
        pre_audit_task = asyncio.create_task(
            _safe_audit_log(sdk, agent_id, "llm.call", {"model": model}, model=str(model), session_id=session_id)
        )

        try:
            response = original(*args, **kwargs)
        except Exception as exc:
            await pre_audit_task
            await _safe_audit_log(
                sdk, agent_id, "llm.error",
                {"model": model, "error_type": type(exc).__name__},
                model=str(model), session_id=session_id,
            )
            raise

        await pre_audit_task

        if is_streaming or _is_streaming_response(response):
            # Item 4: budget gate already ran above. Track projected tokens.
            await _handle_streaming_response(
                sdk, agent_id, session_id, response, model, _projected
            )
            return response

        usage = _extract_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        resp_model = getattr(response, "model", model)
        usd = _estimate_usd(str(resp_model), usage)

        # Run post-call audit + cost track concurrently
        post_coros: list[Any] = [
            _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": resp_model, "token_usage": usage},
                model=str(resp_model), session_id=session_id,
            ),
        ]
        if total_tokens > 0:
            post_coros.append(
                _safe_cost_track(sdk, agent_id, session_id, total_tokens, usd)
            )
        await asyncio.gather(*post_coros)

        return response

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _has_running_loop():
            raise RuntimeError(
                "GovernanceSDK: sync Anthropic wrapper called inside a running "
                "event loop. Use the async Anthropic client (AsyncAnthropic) "
                "with wrap_anthropic() instead, or call from outside the event loop."
            )

        model = kwargs.get("model", "unknown")
        is_streaming = kwargs.get("stream", False)

        # Model selection policy — advisory, sync path
        if getattr(sdk, "routing", None) is not None and sdk.routing.has_policies():
            _suggested = asyncio.run(sdk.routing.suggest(
                agent_id, session_id, str(model),
                max_tokens=int(kwargs.get("max_tokens", 1000)),
            ))
            if _suggested != str(model):
                kwargs["model"] = _suggested
                model = _suggested

        # Enforcement gate 1: scope
        asyncio.run(_scope_check_if_registered(sdk, agent_id, _MESSAGES_CREATE_SENTINEL))

        # Enforcement gate 2: budget with forward projection
        _projected = _resolve_projected_tokens(sdk, kwargs)
        asyncio.run(sdk.cost.check_or_raise(agent_id, session_id, projected_tokens=_projected))

        asyncio.run(_safe_audit_log(sdk, agent_id, "llm.call", {"model": model}, model=str(model), session_id=session_id))

        try:
            response = original(*args, **kwargs)
        except Exception as exc:
            asyncio.run(
                _safe_audit_log(
                    sdk, agent_id, "llm.error",
                    {"model": model, "error_type": type(exc).__name__},
                    model=str(model), session_id=session_id,
                )
            )
            raise

        if is_streaming or _is_streaming_response(response):
            # Item 4: budget gate already ran above. Track projected tokens.
            tracked_tokens = _projected or 0
            if tracked_tokens > 0:
                asyncio.run(_safe_cost_track(sdk, agent_id, session_id, tracked_tokens, 0.0))
                asyncio.run(
                    _safe_audit_log(
                        sdk, agent_id, "llm.result",
                        {"model": model, "streaming": True, "cost_tracked": True,
                         "projected_tokens_tracked": tracked_tokens},
                        model=str(model), session_id=session_id,
                    )
                )
            else:
                logger.warning(
                    "governance.anthropic_wrap.streaming_no_usage",
                    agent_id=agent_id,
                    detail=(
                        "Streaming call with no max_tokens declared and no SDK default. "
                        "Cost not tracked for this call. Pass max_tokens= in your API "
                        "call or set GovernanceSDK(..., default_max_tokens=N)."
                    ),
                )
                asyncio.run(
                    _safe_audit_log(
                        sdk, agent_id, "llm.result",
                        {"model": model, "streaming": True, "cost_tracked": False},
                        model=str(model), session_id=session_id,
                    )
                )
            return response

        usage = _extract_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        resp_model = getattr(response, "model", model)
        usd = _estimate_usd(str(resp_model), usage)

        asyncio.run(
            _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": resp_model, "token_usage": usage},
                model=str(resp_model), session_id=session_id,
            )
        )
        if total_tokens > 0:
            asyncio.run(_safe_cost_track(sdk, agent_id, session_id, total_tokens, usd))

        return response

    return wrapper


def _wrap_async_create(
    original: Any,
    sdk: GovernanceSDK,
    agent_id: str,
    session_id: UUID,
) -> Any:
    """Wrap an async messages.create method."""

    @functools.wraps(original)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model", "unknown")
        is_streaming = kwargs.get("stream", False)

        # Model selection policy — advisory, never raises
        if getattr(sdk, "routing", None) is not None and sdk.routing.has_policies():
            _suggested = await sdk.routing.suggest(
                agent_id, session_id, str(model),
                max_tokens=int(kwargs.get("max_tokens", 1000)),
            )
            if _suggested != str(model):
                kwargs["model"] = _suggested
                model = _suggested

        # Enforcement gate 1: scope — raises ScopeViolation if denied.
        # Fires before cost so a scope denial never touches the budget counter.
        await _scope_check_if_registered(sdk, agent_id, _MESSAGES_CREATE_SENTINEL)

        # Enforcement gate 2: budget — raises BudgetExceeded if over cap.
        # Pass projected_tokens so the gate blocks calls that would exceed
        # the limit, not just calls that already exceeded it.
        _projected = _resolve_projected_tokens(sdk, kwargs)
        await sdk.cost.check_or_raise(agent_id, session_id, projected_tokens=_projected)

        # Observation: audit log pre-call — backgrounded because it is
        # observation-only, not an enforcement gate.  We hold a reference
        # to the task to avoid silent failures (no fire-and-forget).
        pre_audit_task = asyncio.create_task(
            _safe_audit_log(sdk, agent_id, "llm.call", {"model": model}, model=str(model), session_id=session_id)
        )

        try:
            response = await original(*args, **kwargs)
        except Exception as exc:
            # Await the pre-call audit before propagating the error so it
            # completes before we log the error event.
            await pre_audit_task
            await _safe_audit_log(
                sdk, agent_id, "llm.error",
                {"model": model, "error_type": type(exc).__name__},
                model=str(model), session_id=session_id,
            )
            raise

        # Ensure pre-call audit completed before post-call work
        await pre_audit_task

        if is_streaming or _is_streaming_response(response):
            # Item 4: pre-stream gate already ran above. Now accumulate tokens post-stream.
            await _handle_streaming_response(
                sdk, agent_id, session_id, response, model, _projected
            )
            return response

        # Observation: audit log + cost track post-call — run concurrently.
        # cost.track must complete before the next check_or_raise to avoid
        # budget race conditions, but it CAN run concurrently with the
        # post-call audit log.
        usage = _extract_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        resp_model = getattr(response, "model", model)
        usd = _estimate_usd(str(resp_model), usage)

        post_coros: list[Any] = [
            _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": resp_model, "token_usage": usage},
                model=str(resp_model), session_id=session_id,
            ),
        ]
        if total_tokens > 0:
            post_coros.append(
                _safe_cost_track(sdk, agent_id, session_id, total_tokens, usd)
            )
        await asyncio.gather(*post_coros)

        return response

    return wrapper


def wrap_anthropic(
    client: Any,
    sdk: GovernanceSDK,
    agent_id: str,
    session_id: UUID | None = None,
) -> Any:
    """Patch an Anthropic client to emit governance audit events.

    Supports both ``anthropic.Anthropic`` (sync) and ``anthropic.AsyncAnthropic``
    (async). The client is monkey-patched in-place and returned so existing
    references continue to work.

    A single ``session_id`` is shared across ALL calls made through this
    wrapped client, ensuring per-session budget limits work correctly.
    Pass an explicit ``session_id`` for deterministic control, or omit it
    to generate one automatically.

    Args:
        client: An ``anthropic.Anthropic`` or ``anthropic.AsyncAnthropic`` instance.
        sdk: The initialized GovernanceSDK instance.
        agent_id: The agent identifier for audit and cost tracking.
        session_id: Optional session UUID; auto-generated if not provided.

    Returns:
        The same client object, patched in-place.
    """
    if getattr(client, "_governance_wrapped", False):
        logger.warning(
            "governance.anthropic_wrap.already_wrapped",
            agent_id=agent_id,
        )
        return client

    sid = session_id if session_id is not None else uuid4()

    messages = client.messages

    is_async = asyncio.iscoroutinefunction(getattr(messages, "create", None))

    if is_async:
        messages.create = _wrap_async_create(messages.create, sdk, agent_id, sid)
    else:
        messages.create = _wrap_sync_create(messages.create, sdk, agent_id, sid)

    client._governance_wrapped = True
    client._governance_session_id = sid

    # Register this wrapper on the SDK so start() can warn when zero wrappers
    # are active, and so callers can verify enforcement is live.
    wrapper_label = f"anthropic:{agent_id}"
    registered: list[str] = getattr(sdk, "_registered_wrappers", [])
    if wrapper_label not in registered:
        registered.append(wrapper_label)
        # If SDK already started, emit an info confirming enforcement is now active.
        if getattr(sdk, "_started", False):
            logger.info(
                "governance.anthropic_wrap.enforcement_active",
                agent_id=agent_id,
                message=f"Scope and budget enforcement now active for agent '{agent_id}' via wrap_anthropic().",
            )

    return client
