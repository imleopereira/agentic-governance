"""OpenAI SDK wrapper that patches a client to emit governance events.

Usage::

    from openai import OpenAI
    from codeatelier_governance import GovernanceSDK
    from codeatelier_governance.integrations.openai_wrap import wrap_openai

    sdk = GovernanceSDK(database_url="postgresql://...")
    client = wrap_openai(OpenAI(), sdk=sdk, agent_id="my-agent")
    # client.chat.completions.create() now emits audit events

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
# OpenAI calls have no explicit tool name here, so we use a stable sentinel.
_CHAT_COMPLETIONS_SENTINEL = "chat.completions.create"


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
    """Extract token usage from an OpenAI response object."""
    usage: dict[str, int] = {}
    if hasattr(response, "usage") and response.usage is not None:
        u = response.usage
        if hasattr(u, "prompt_tokens"):
            usage["prompt_tokens"] = int(u.prompt_tokens)
        if hasattr(u, "completion_tokens"):
            usage["completion_tokens"] = int(u.completion_tokens)
        if hasattr(u, "total_tokens"):
            usage["total_tokens"] = int(u.total_tokens)
    return usage


def _estimate_usd(model: str, usage: dict[str, int]) -> float:
    """Estimate USD cost using the built-in pricing table.

    Returns 0.0 if the model is unknown or usage is empty.
    """
    from codeatelier_governance.cost.pricing import estimate_cost

    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    return estimate_cost(model, prompt, completion)


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

    A ``max_tokens`` value of 0 or negative is meaningless for forward budget
    projection and is treated as absent — the SDK falls through to
    ``default_max_tokens`` or emits the "max_tokens not declared" warning.

    Args:
        sdk: The GovernanceSDK instance.
        kwargs: The keyword arguments passed to completions.create.

    Returns:
        The projected token count, or None if unavailable.
    """
    if "max_tokens" in kwargs:
        max_tokens: int | None = int(kwargs["max_tokens"])
        if max_tokens is not None and max_tokens <= 0:
            # max_tokens=0 or negative is meaningless for projection; treat as absent.
            max_tokens = None
        if max_tokens is not None:
            return max_tokens
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
            "governance.openai_wrap.streaming_no_usage",
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
            "governance.openai_wrap.audit_log_failed",
            kind=kind,
            error_type=type(exc).__name__,
        )


async def _safe_cost_track(sdk: GovernanceSDK, agent_id: str, session_id: UUID, tokens: int, usd: float) -> None:
    """Track cost, swallowing all errors (observation surface)."""
    try:
        await sdk.cost.track(agent_id, session_id, tokens=tokens, usd=usd)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "governance.openai_wrap.cost_track_failed",
            error_type=type(exc).__name__,
        )


def _is_streaming_response(response: Any) -> bool:
    """Detect OpenAI streaming response objects (Stream / AsyncStream)."""
    type_name = type(response).__name__
    return type_name in ("Stream", "AsyncStream")


def _wrap_sync_create(
    original: Any,
    sdk: GovernanceSDK,
    agent_id: str,
    session_id: UUID,
) -> Any:
    """Wrap a sync chat.completions.create method.

    If called from within an existing async event loop (e.g. from a test
    or a framework that nests sync in async), the wrapper raises a clear
    error directing the developer to use the async API. When there is no
    running loop, it uses ``asyncio.run`` for each governance call.
    """

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _has_running_loop():
            raise RuntimeError(
                "GovernanceSDK: sync OpenAI wrapper called inside a running "
                "event loop. Use the async OpenAI client (AsyncOpenAI) with "
                "wrap_openai() instead, or call from outside the event loop."
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
        asyncio.run(_scope_check_if_registered(sdk, agent_id, _CHAT_COMPLETIONS_SENTINEL))

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
            # Streaming: budget gate already fired above. Audit with cost_tracked=True
            # (post-stream tracking is not feasible in the sync path without consuming
            # the generator, so we fall back to max_tokens as projected usage).
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
                    "governance.openai_wrap.streaming_no_usage",
                    agent_id=agent_id,
                    detail="Streaming call with no max_tokens; cost not tracked for this call.",
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
        usd = _estimate_usd(str(model), usage)

        asyncio.run(
            _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": model, "token_usage": usage},
                model=str(model), session_id=session_id,
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
    """Wrap an async chat.completions.create method."""

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
        await _scope_check_if_registered(sdk, agent_id, _CHAT_COMPLETIONS_SENTINEL)

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
        except BaseException as exc:
            # Await the pre-call audit before propagating the error so it
            # completes before we log the error event.
            # BaseException is used (not Exception) so that asyncio.CancelledError
            # (which inherits from BaseException in Python 3.8+) is also audited.
            # CancelledError is always re-raised by the final `raise`.
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
            # Item 4: pre-stream gate already ran above. Now handle post-stream tracking.
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
        usd = _estimate_usd(str(model), usage)

        post_coros: list[Any] = [
            _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": model, "token_usage": usage},
                model=str(model), session_id=session_id,
            ),
        ]
        if total_tokens > 0:
            post_coros.append(
                _safe_cost_track(sdk, agent_id, session_id, total_tokens, usd)
            )
        await asyncio.gather(*post_coros)

        return response

    return wrapper


def wrap_openai(
    client: Any,
    sdk: GovernanceSDK,
    agent_id: str,
    session_id: UUID | None = None,
) -> Any:
    """Patch an OpenAI client to emit governance audit events.

    Supports both ``openai.OpenAI`` (sync) and ``openai.AsyncOpenAI`` (async).
    The client is monkey-patched in-place and returned so existing references
    continue to work.

    A single ``session_id`` is shared across ALL calls made through this
    wrapped client, ensuring per-session budget limits work correctly.
    Pass an explicit ``session_id`` for deterministic control, or omit it
    to generate one automatically.

    Args:
        client: An ``openai.OpenAI`` or ``openai.AsyncOpenAI`` instance.
        sdk: The initialized GovernanceSDK instance.
        agent_id: The agent identifier for audit and cost tracking.
        session_id: Optional session UUID; auto-generated if not provided.

    Returns:
        The same client object, patched in-place.

    Raises:
        ValueError: If ``agent_id`` is empty or whitespace-only.
    """
    if not agent_id or not agent_id.strip():
        raise ValueError(
            f"agent_id must be a non-empty string, got {agent_id!r}"
        )

    if getattr(client, "_governance_wrapped", False):
        logger.warning(
            "governance.openai_wrap.already_wrapped",
            agent_id=agent_id,
        )
        return client

    sid = session_id if session_id is not None else uuid4()

    completions = client.chat.completions

    # OpenAI SDK's AsyncCompletions.create is not detected by iscoroutinefunction
    # (it's a method descriptor, not a plain coroutine function). Check the class
    # name or whether the client type contains "Async" as a more reliable signal.
    is_async = (
        asyncio.iscoroutinefunction(getattr(completions, "create", None))
        or "Async" in type(client).__name__
    )

    if is_async:
        completions.create = _wrap_async_create(completions.create, sdk, agent_id, sid)
    else:
        completions.create = _wrap_sync_create(completions.create, sdk, agent_id, sid)

    client._governance_wrapped = True
    client._governance_session_id = sid

    # Register this wrapper on the SDK so start() can warn when zero wrappers
    # are active, and so callers can verify enforcement is live.
    wrapper_label = f"openai:{agent_id}"
    registered: list[str] = getattr(sdk, "_registered_wrappers", [])
    if wrapper_label not in registered:
        registered.append(wrapper_label)
        # F9: record this wrapper in the coverage registry. Fire-and-forget:
        # a registry error must never propagate into host code (invariant #1).
        try:
            registry = getattr(sdk, "_wrapper_registry", None)
            if registry is not None:
                registry.register(agent_id, "openai")
        except Exception as _reg_exc:  # noqa: BLE001
            logger.warning(
                "governance.openai_wrap.coverage_register_failed",
                agent_id=agent_id,
                error_type=type(_reg_exc).__name__,
            )
        # If SDK already started, emit an info confirming enforcement is now active.
        if getattr(sdk, "_started", False):
            logger.info(
                "governance.openai_wrap.enforcement_active",
                agent_id=agent_id,
                message=f"Scope and budget enforcement now active for agent '{agent_id}' via wrap_openai().",
            )

    return client
