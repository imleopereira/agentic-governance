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
import weakref
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

# v0.6.2-followup (Bug #8 hardening): conservative per-chunk token estimate for
# torn-down streams. See anthropic_wrap.STREAM_CHUNK_TOKEN_ESTIMATE for
# rationale (over-count on uncertainty; under-counting = silent bypass).
STREAM_CHUNK_TOKEN_ESTIMATE = 8

# v0.6.2-followup: strong references to pending reconcile tasks spawned from
# sync __exit__ / __iter__ paths so the asyncio task isn't GC'd mid-execution.
_PENDING_RECONCILES: "set[asyncio.Task[Any]]" = set()


def _spawn_tracked_reconcile(loop: asyncio.AbstractEventLoop, coro: Any) -> None:
    """Spawn a reconcile coroutine as a task with a strong ref held until done."""
    task = loop.create_task(coro)
    _PENDING_RECONCILES.add(task)
    task.add_done_callback(_PENDING_RECONCILES.discard)


def _inject_include_usage(kwargs: dict[str, Any], agent_id: str) -> None:
    """Auto-inject ``stream_options={'include_usage': True}`` when streaming.

    v0.6.2-followup (Bypass #4): previously the wrapper required the caller
    to opt into ``stream_options={"include_usage": True}``. Callers who
    forgot got silent under-counting: OpenAI never emits a final usage chunk,
    ``_extract_chunk_usage`` returns None, reconcile warns but tracked stays
    at the projected amount. That's the bypass.

    Fix: inject by default for streaming calls. Respect explicit user intent —
    if the caller set ``include_usage: False``, we warn but don't override.
    """
    if not kwargs.get("stream"):
        return
    opts = dict(kwargs.get("stream_options") or {})
    if "include_usage" not in opts:
        opts["include_usage"] = True
        kwargs["stream_options"] = opts
    elif opts["include_usage"] is False:
        logger.warning(
            "governance.openai_wrap.include_usage_explicitly_disabled",
            agent_id=agent_id,
            detail=(
                "Caller explicitly set stream_options={'include_usage': False}. "
                "Governance honors this, but streaming reconciliation cannot "
                "read actual usage and tracked amount will remain the "
                "projected value. Remove the False override to enable "
                "accurate reconciliation."
            ),
        )


async def _halt_check_if_wired(sdk: Any, agent_id: str) -> None:
    """Fail-closed halt check, run before every enforcement gate (v0.6.2 P0).

    If the SDK has a PresenceModule wired, call ``assert_not_halted(agent_id)``
    so an operator halt blocks the LLM call BEFORE it touches the network.
    Silent no-op if presence is not configured (back-compat with SDKs built
    without ``enable_presence``). Closes the v0.5.4 bypass where halt only
    fired via scope.check and therefore only hit agents with a registered
    scope policy.
    """
    presence = getattr(sdk, "presence", None)
    if presence is None:
        return
    await presence.assert_not_halted(agent_id)


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


def _estimate_usd(sdk: Any, model: str, usage: dict[str, int]) -> float:
    """Estimate USD cost using the built-in pricing table.

    Post-call observation surface — MUST NOT raise by contract. We respect
    the CostModule's ``strict_unknown_models`` flag but catch
    :class:`UnknownModelError` and log at ``error`` level so the caller
    sees the misconfiguration without breaking the host call. In strict
    mode on unknown model we return 0.0 AND log an error (operator visibility).
    """
    from codeatelier_governance.cost.errors import UnknownModelError
    from codeatelier_governance.cost.pricing import estimate_cost

    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    cost = getattr(sdk, "cost", None)
    strict = bool(getattr(cost, "_strict_unknown_models", False))
    fallback = getattr(cost, "_unknown_model_fallback_usd_per_million", None)
    try:
        return estimate_cost(
            model,
            prompt,
            completion,
            strict=strict,
            fallback_usd_per_million=fallback,
        )
    except UnknownModelError as exc:
        logger.error(
            "governance.openai_wrap.unknown_model",
            model=model,
            error=str(exc),
            detail=(
                "Unknown model name encountered in post-call cost accounting. "
                "USD budget caps will not count this call. Add the model to "
                "MODEL_PRICING or set strict_unknown_models=False with a "
                "non-zero fallback rate on CostModule."
            ),
        )
        return 0.0


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
) -> Any:
    """Track projected tokens at stream start; reconcile when stream completes.

    Two-phase cost accounting for streams (v0.6.2 Bug #8 fix):
      1. Track ``projected_tokens`` (max_tokens or SDK default) so the
         forward-looking budget gate sees projected spend.
      2. Return a reconciliation proxy that reads the final ``usage``
         chunk (requires ``stream_options={'include_usage': True}``) and
         tracks the DELTA via :meth:`CostModule.reconcile`.

    Previously the wrapper trusted ``max_tokens`` forever — a stream with
    10 max_tokens but 10k actual output would undercount by 1000x, a USD
    cap bypass vector.

    Returns:
        The stream object, wrapped with a reconciliation proxy. The caller
        uses the proxy in place of the raw stream.
    """
    if projected_tokens is not None and projected_tokens > 0:
        await _safe_cost_track(sdk, agent_id, session_id, projected_tokens, 0.0)
        await _safe_audit_log(
            sdk, agent_id, "llm.result",
            {"model": model, "streaming": True, "cost_tracked": True,
             "projected_tokens_tracked": projected_tokens,
             "reconciliation": "pending"},
            model=str(model), session_id=session_id,
        )
        return _wrap_openai_stream_for_reconciliation(
            stream, sdk, agent_id, session_id, model, projected_tokens,
        )
    logger.warning(
        "governance.openai_wrap.streaming_no_usage",
        agent_id=agent_id,
        detail=(
            "Streaming call with no max_tokens declared and no SDK default. "
            "Cost not tracked upfront — will be reconciled against the "
            "final provider usage chunk when the stream ends."
        ),
    )
    await _safe_audit_log(
        sdk, agent_id, "llm.result",
        {"model": model, "streaming": True, "cost_tracked": False,
         "reconciliation": "pending"},
        model=str(model), session_id=session_id,
    )
    return _wrap_openai_stream_for_reconciliation(
        stream, sdk, agent_id, session_id, model, 0,
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


def _extract_chunk_usage(chunk: Any) -> dict[str, int] | None:
    """If an OpenAI stream chunk carries a final ``usage`` block, extract it.

    OpenAI emits the final-usage chunk only when the caller passes
    ``stream_options={"include_usage": True}``. When absent, we cannot
    reconcile accurately and fall back to the projected tokens.
    """
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    out: dict[str, int] = {}
    for src, dst in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        val = getattr(usage, src, None)
        if val is not None:
            out[dst] = int(val)
    if "total_tokens" not in out and {"prompt_tokens", "completion_tokens"} <= set(out):
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out or None


async def _reconcile_openai_stream(
    sdk: Any,
    agent_id: str,
    session_id: UUID,
    final_usage: dict[str, int] | None,
    model: str,
    projected_tokens: int,
    *,
    chunks_seen: int = 0,
    torn_down: bool = False,
) -> None:
    """Reconcile a finished OpenAI stream against the provider's final usage.

    v0.6.2-followup: when the stream was ``torn_down`` (cancellation / mid-stream
    exception) and no final usage chunk was captured, fall back to a conservative
    per-chunk token estimate (``chunks_seen * STREAM_CHUNK_TOKEN_ESTIMATE``)
    floored at ``projected_tokens``. Over-counts on purpose to close the bypass.
    """
    reconciliation_source = "provider_final"
    if final_usage is None or "total_tokens" not in final_usage:
        if torn_down and chunks_seen > 0:
            estimated_actual = max(
                projected_tokens,
                chunks_seen * STREAM_CHUNK_TOKEN_ESTIMATE,
            )
            final_usage = {
                "prompt_tokens": 0,
                "completion_tokens": estimated_actual,
                "total_tokens": estimated_actual,
            }
            reconciliation_source = "chunk_estimate"
            logger.warning(
                "governance.openai_wrap.stream_usage_estimated",
                agent_id=agent_id,
                model=model,
                projected_tokens=projected_tokens,
                chunks_seen=chunks_seen,
                estimated_actual=estimated_actual,
                detail=(
                    "OpenAI stream torn down before final usage chunk; "
                    "estimating actual tokens from chunk count. Over-counts "
                    "by design to avoid silent budget bypass."
                ),
            )
        else:
            logger.warning(
                "governance.openai_wrap.stream_usage_unavailable",
                agent_id=agent_id,
                model=model,
                projected_tokens=projected_tokens,
                detail=(
                    "OpenAI stream did not expose a final usage chunk. Pass "
                    "stream_options={'include_usage': True} to enable accurate "
                    "reconciliation; tracked amount remains the projected value."
                ),
            )
            return
    actual_tokens = int(final_usage["total_tokens"])
    actual_usd = _estimate_usd(sdk, model, final_usage)
    cost = getattr(sdk, "cost", None)
    if cost is None or not hasattr(cost, "reconcile"):
        token_delta = max(0, actual_tokens - projected_tokens)
        if token_delta > 0 or actual_usd > 0.0:
            await _safe_cost_track(
                sdk, agent_id, session_id, token_delta, actual_usd,
            )
    else:
        try:
            await cost.reconcile(
                agent_id,
                session_id,
                projected_tokens=projected_tokens,
                actual_tokens=actual_tokens,
                projected_usd=0.0,
                actual_usd=actual_usd,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "governance.openai_wrap.reconcile_failed",
                agent_id=agent_id,
                error_type=type(exc).__name__,
            )
    await _safe_audit_log(
        sdk, agent_id, "llm.result.reconciled",
        {
            "model": model,
            "streaming": True,
            "projected_tokens": projected_tokens,
            "actual_tokens": actual_tokens,
            "actual_usd": actual_usd,
            "token_delta": actual_tokens - projected_tokens,
            "reconciliation_source": reconciliation_source,
        },
        model=str(model), session_id=session_id,
    )


def _openai_abandonment_finalizer(
    state: dict[str, Any],
    agent_id: str,
    session_id: UUID,
    projected_tokens: int,
    model: str,
) -> None:
    """Emit WARN when an OpenAI stream proxy is GC'd without iteration.

    MUST NOT capture the proxy in its closure — weakref.finalize passes the
    state dict as an arg so the proxy can actually be collected.
    """
    if state.get("reconciled"):
        return
    state["reconciled"] = True
    logger.warning(
        "governance.stream.abandoned_without_reconcile",
        agent_id=agent_id,
        session_id=str(session_id),
        projected_tokens=projected_tokens,
        model=model,
        detail=(
            "OpenAI stream proxy was garbage-collected without iteration — "
            "final usage was never read. Tracked amount remains the projected "
            "(max_tokens) value. To fix: iterate the stream or call close()."
        ),
    )


def _wrap_openai_stream_for_reconciliation(
    stream: Any,
    sdk: Any,
    agent_id: str,
    session_id: UUID,
    model: str,
    projected_tokens: int,
    state: dict[str, Any] | None = None,
) -> Any:
    """Proxy an OpenAI stream so we reconcile usage when iteration ends.

    v0.6.2-followup — closes bypass vectors found in DA review:
      * Abandonment: weakref.finalize emits WARN on GC without iteration.
      * Cancellation / mid-stream exception: split except/else so
        CancelledError (BaseException) triggers estimate-based reconcile AND
        is re-raised.
      * isinstance transparency: __class__ reports wrapped type.

    The terminal chunk optionally carries ``.usage`` when
    ``stream_options={'include_usage': True}``. The outer wrapper auto-injects
    that option for streaming calls (see :func:`_inject_include_usage`) so
    reconciliation works by default.
    """
    if state is None:
        state = {"last_usage": None, "reconciled": False, "chunks_seen": 0}
    # Rebind to a non-optional alias so the nested class methods that
    # close over ``state`` see a ``dict[str, Any]`` (mypy loses Optional
    # narrowing across nested class scopes).
    st: dict[str, Any] = state
    has_aiter = hasattr(stream, "__aiter__")
    has_iter = hasattr(stream, "__iter__")
    _wrapped_class = type(stream)

    async def _run_reconcile(torn_down: bool = False) -> None:
        if st["reconciled"]:
            return
        st["reconciled"] = True
        await _reconcile_openai_stream(
            sdk, agent_id, session_id,
            st["last_usage"], model, projected_tokens,
            chunks_seen=st.get("chunks_seen", 0),
            torn_down=torn_down,
        )

    class _ReconcilingProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(stream, name)

        def __repr__(self) -> str:
            return f"<OpenAIReconcilingProxy wrap={stream!r}>"

        # isinstance transparency: report the wrapped stream's class so
        # callers doing ``isinstance(proxy, ChatCompletionChunk)`` succeed.
        # Overriding ``__class__`` via a read-only @property is intentional
        # — object.__class__ is modelled as read-write in the mypy stub,
        # so the override is flagged ``[misc]``.
        @property  # type: ignore[misc]
        def __class__(self) -> type:
            return _wrapped_class

        if has_aiter:
            def __aiter__(self) -> Any:
                async def _gen() -> Any:
                    try:
                        async for chunk in stream:
                            st["chunks_seen"] = st.get("chunks_seen", 0) + 1
                            chunk_usage = _extract_chunk_usage(chunk)
                            if chunk_usage is not None:
                                st["last_usage"] = chunk_usage
                            yield chunk
                    except BaseException:
                        # CancelledError inherits BaseException; reconcile with
                        # torn_down=True then re-raise so cancellation actually
                        # propagates.
                        try:
                            await _run_reconcile(torn_down=True)
                        except Exception as _exc:  # noqa: BLE001
                            logger.error(
                                "governance.openai_wrap.reconcile_aiter_failed",
                                error_type=type(_exc).__name__,
                            )
                        raise
                    else:
                        try:
                            await _run_reconcile(torn_down=False)
                        except Exception as _exc:  # noqa: BLE001
                            logger.error(
                                "governance.openai_wrap.reconcile_aiter_failed",
                                error_type=type(_exc).__name__,
                            )
                return _gen()

        if has_iter:
            def __iter__(self) -> Any:
                torn_down = False
                try:
                    for chunk in stream:
                        st["chunks_seen"] = st.get("chunks_seen", 0) + 1
                        chunk_usage = _extract_chunk_usage(chunk)
                        if chunk_usage is not None:
                            st["last_usage"] = chunk_usage
                        yield chunk
                except BaseException:
                    torn_down = True
                    raise
                finally:
                    try:
                        if _has_running_loop():
                            loop = asyncio.get_running_loop()
                            _spawn_tracked_reconcile(
                                loop, _run_reconcile(torn_down=torn_down),
                            )
                        else:
                            asyncio.run(_run_reconcile(torn_down=torn_down))
                    except Exception as _exc:  # noqa: BLE001
                        logger.error(
                            "governance.openai_wrap.reconcile_iter_failed",
                            error_type=type(_exc).__name__,
                        )

    proxy = _ReconcilingProxy()
    weakref.finalize(
        proxy,
        _openai_abandonment_finalizer,
        state, agent_id, session_id, projected_tokens, model,
    )
    return proxy


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

        # v0.6.2-followup (Bypass #4): auto-inject stream_options={include_usage:True}
        # for streaming calls so reconciliation works by default. Respects explicit
        # caller intent — honors include_usage=False with a WARN.
        if is_streaming:
            _inject_include_usage(kwargs, agent_id)

        # Model selection policy — advisory, sync path
        if getattr(sdk, "routing", None) is not None and sdk.routing.has_policies():
            _suggested = asyncio.run(sdk.routing.suggest(
                agent_id, session_id, str(model),
                max_tokens=int(kwargs.get("max_tokens", 1000)),
            ))
            if _suggested != str(model):
                kwargs["model"] = _suggested
                model = _suggested

        # Enforcement gate 0: halt switch (v0.6.2 P0).
        # Runs BEFORE scope/cost so a halted agent's LLM call never fires,
        # even when no scope or budget policy is registered.
        asyncio.run(_halt_check_if_wired(sdk, agent_id))

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
            # Streaming: budget gate already fired above. Track projected
            # upfront, then return a reconciliation-wrapped stream so the
            # provider's final usage chunk lands as a delta (Bug #8 fix).
            # Reconciliation requires the caller to pass
            # stream_options={'include_usage': True} — otherwise the
            # wrapper logs a warning and tracked amount remains projected.
            tracked_tokens = _projected or 0
            if tracked_tokens > 0:
                asyncio.run(_safe_cost_track(sdk, agent_id, session_id, tracked_tokens, 0.0))
                asyncio.run(
                    _safe_audit_log(
                        sdk, agent_id, "llm.result",
                        {"model": model, "streaming": True, "cost_tracked": True,
                         "projected_tokens_tracked": tracked_tokens,
                         "reconciliation": "pending"},
                        model=str(model), session_id=session_id,
                    )
                )
            else:
                logger.warning(
                    "governance.openai_wrap.streaming_no_usage",
                    agent_id=agent_id,
                    detail="Streaming call with no max_tokens; will reconcile from final usage chunk.",
                )
                asyncio.run(
                    _safe_audit_log(
                        sdk, agent_id, "llm.result",
                        {"model": model, "streaming": True, "cost_tracked": False,
                         "reconciliation": "pending"},
                        model=str(model), session_id=session_id,
                    )
                )
            return _wrap_openai_stream_for_reconciliation(
                response, sdk, agent_id, session_id, str(model), tracked_tokens,
            )

        usage = _extract_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        usd = _estimate_usd(sdk, str(model), usage)

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

        # v0.6.2-followup (Bypass #4): auto-inject stream_options={include_usage:True}
        # for streaming calls BEFORE the LLM call so OpenAI emits the final usage
        # chunk and reconciliation can read actual tokens. Honors an explicit
        # include_usage=False with a WARN (never overrides user intent).
        if is_streaming:
            _inject_include_usage(kwargs, agent_id)

        # Model selection policy — advisory, never raises
        if getattr(sdk, "routing", None) is not None and sdk.routing.has_policies():
            _suggested = await sdk.routing.suggest(
                agent_id, session_id, str(model),
                max_tokens=int(kwargs.get("max_tokens", 1000)),
            )
            if _suggested != str(model):
                kwargs["model"] = _suggested
                model = _suggested

        # Enforcement gate 0: halt switch (v0.6.2 P0). Fires BEFORE scope/cost
        # so a halted agent's LLM call never fires, even when no scope or
        # budget policy is registered. Closes the v0.5.4 bypass.
        await _halt_check_if_wired(sdk, agent_id)

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
            # Item 4: pre-stream gate already ran above. Track projected
            # tokens upfront and return a reconciliation-wrapped stream so
            # the final usage chunk is reconciled post-stream (Bug #8 fix).
            wrapped = await _handle_streaming_response(
                sdk, agent_id, session_id, response, model, _projected
            )
            return wrapped

        # Observation: audit log + cost track post-call — run concurrently.
        # cost.track must complete before the next check_or_raise to avoid
        # budget race conditions, but it CAN run concurrently with the
        # post-call audit log.
        usage = _extract_token_usage(response)
        total_tokens = usage.get("total_tokens", 0)
        usd = _estimate_usd(sdk, str(model), usage)

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
