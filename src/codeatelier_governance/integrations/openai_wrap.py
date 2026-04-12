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
surface (cost.check_or_raise) DOES raise BudgetExceeded by contract.
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

        await sdk.cost.check_or_raise(agent_id, session_id)

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
            logger.warning(
                "governance.openai_wrap.streaming_bypass",
                agent_id=agent_id,
                model=model,
                detail=(
                    "stream=True bypasses cost tracking. Token usage is not "
                    "available until the stream is fully consumed. Call "
                    "sdk.cost.track() manually after consuming the stream, "
                    "or use stream_options={'include_usage': True} and read "
                    "usage from the final chunk."
                ),
            )
            await _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": model, "streaming": True, "cost_tracked": False},
                model=str(model), session_id=session_id,
            )
            return response

        usage = _extract_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        usd = _estimate_usd(str(model), usage)

        # Run post-call audit + cost track concurrently
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

        asyncio.run(sdk.cost.check_or_raise(agent_id, session_id))

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
            logger.warning(
                "governance.openai_wrap.streaming_bypass",
                agent_id=agent_id,
                model=model,
                detail=(
                    "stream=True bypasses cost tracking. Token usage is not "
                    "available until the stream is fully consumed. Call "
                    "sdk.cost.track() manually after consuming the stream, "
                    "or use stream_options={'include_usage': True} and read "
                    "usage from the final chunk."
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

        # Enforcement: check budget BEFORE the call (must stay on critical path).
        await sdk.cost.check_or_raise(agent_id, session_id)

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
            logger.warning(
                "governance.openai_wrap.streaming_bypass",
                agent_id=agent_id,
                model=model,
                detail=(
                    "stream=True bypasses cost tracking. Token usage is not "
                    "available until the stream is fully consumed. Call "
                    "sdk.cost.track() manually after consuming the stream, "
                    "or use stream_options={'include_usage': True} and read "
                    "usage from the final chunk."
                ),
            )
            await _safe_audit_log(
                sdk, agent_id, "llm.result",
                {"model": model, "streaming": True, "cost_tracked": False},
                model=str(model), session_id=session_id,
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
    """
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
    return client
