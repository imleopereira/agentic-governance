"""v0.6.2 followup upgrade-breaker: LangChain handler must not raise.

Pre-followup, ``GovernanceCallbackHandler`` called
``estimate_cost(model, in, out)`` positionally — which defaults to
``strict=True`` as of v0.6.2 (to close the silent-zero
budget-bypass). Any LangChain customer with a fine-tuned or
custom-named model (``my-ft-gpt4``) got ``UnknownModelError`` on
EVERY token-usage callback, crashing their callback pipeline.

Fix: the handler routes through ``_estimate_cost_safe`` which
always passes ``strict=False`` and inherits the SDK's CostModule
``_unknown_model_fallback_usd_per_million`` when cost is enabled.

Handler invariant: NEVER raise to LangChain. That's the
observation-surface contract — scope, cost, and audit may fail
internally, but the callback body must not propagate exceptions.
"""
from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4


def _fake_sdk_with_cost(fallback: float | None) -> MagicMock:
    sdk = MagicMock()
    cost = MagicMock()
    cost._unknown_model_fallback_usd_per_million = fallback
    sdk.cost = cost
    return sdk


def test_unknown_model_does_not_raise() -> None:
    """Handler helper returns 0.0 (or fallback) rather than raising."""
    from codeatelier_governance.integrations.langchain_handler import (
        GovernanceCallbackHandler,
    )

    sdk = _fake_sdk_with_cost(fallback=None)
    handler = GovernanceCallbackHandler(
        sdk=sdk, agent_id="ft-agent", session_id=uuid4(),
    )

    # Should NOT raise UnknownModelError; should NOT raise anything.
    usd = handler._estimate_cost_safe("my-ft-gpt4-v7", 1000, 500)
    assert isinstance(usd, float)
    assert usd == 0.0, "no fallback configured → 0.0 (documented foot-gun, warn fires)"


def test_unknown_model_uses_configured_fallback() -> None:
    """Operator-configured fallback rate is applied to unknown models."""
    from codeatelier_governance.integrations.langchain_handler import (
        GovernanceCallbackHandler,
    )

    # $5 per million tokens, 1500 total → $0.0075
    sdk = _fake_sdk_with_cost(fallback=5.0)
    handler = GovernanceCallbackHandler(
        sdk=sdk, agent_id="ft-agent", session_id=uuid4(),
    )

    usd = handler._estimate_cost_safe("my-ft-gpt4-v7", 1000, 500)
    assert usd > 0.0, (
        "configured fallback must propagate — otherwise customers with "
        "fine-tuned names get silent-zero budgets, the exact P0 vector "
        "the strict default was meant to close"
    )
    # $5 / 1_000_000 * (1000 + 500) = 0.0075
    assert abs(usd - 0.0075) < 1e-9


def test_known_model_still_prices_correctly() -> None:
    """Strict-false mode must not regress pricing for known models."""
    from codeatelier_governance.integrations.langchain_handler import (
        GovernanceCallbackHandler,
    )

    sdk = _fake_sdk_with_cost(fallback=None)
    handler = GovernanceCallbackHandler(
        sdk=sdk, agent_id="ok-agent", session_id=uuid4(),
    )

    # A well-known model should get its real price, not the 0.0 fallback.
    usd = handler._estimate_cost_safe("gpt-4o", 1000, 500)
    assert usd > 0.0, (
        "known model returned 0.0 — safe helper must not over-apply "
        "its fallback to known models"
    )


def test_unknown_model_warn_is_rate_limited_per_handler() -> None:
    """One warn per unique unknown model per handler lifetime."""
    from codeatelier_governance.integrations import langchain_handler
    from codeatelier_governance.integrations.langchain_handler import (
        GovernanceCallbackHandler,
    )

    sdk = _fake_sdk_with_cost(fallback=0.0)
    handler = GovernanceCallbackHandler(
        sdk=sdk, agent_id="ft-agent", session_id=uuid4(),
    )

    captured: list[tuple[str, dict]] = []

    def _fake_warn(event: str, **kwargs: object) -> None:
        captured.append((event, dict(kwargs)))

    original = langchain_handler.logger.warning
    langchain_handler.logger.warning = _fake_warn  # type: ignore[assignment]
    try:
        # 50 calls with the same unknown model → 1 handler-level warn.
        for _ in range(50):
            handler._estimate_cost_safe("my-ft-gpt4-v7", 10, 5)
        # A second unknown model → 1 more handler-level warn.
        for _ in range(10):
            handler._estimate_cost_safe("another-ft-model", 10, 5)
    finally:
        langchain_handler.logger.warning = original  # type: ignore[assignment]

    handler_warns = [
        c for c in captured
        if c[0] == "governance.langchain_handler.unknown_model"
    ]
    unique_models = {c[1].get("model") for c in handler_warns}
    assert len(handler_warns) == 2, (
        f"expected 2 handler-level warns (one per unique model), got "
        f"{len(handler_warns)} — rate limiter broken"
    )
    assert unique_models == {"my-ft-gpt4-v7", "another-ft-model"}


def test_handler_without_cost_module_still_safe() -> None:
    """If sdk.cost is missing (enable_cost=False), helper still works."""
    from codeatelier_governance.integrations.langchain_handler import (
        GovernanceCallbackHandler,
    )

    sdk = MagicMock()
    # Simulate enable_cost=False — cost attribute is not set
    # (using spec=[] to ensure hasattr returns False).
    del sdk.cost
    # getattr(sdk, "cost", None) must return None for the helper.
    type(sdk).cost = property(
        fget=lambda self: (_ for _ in ()).throw(AttributeError())
    )

    # Construct once to confirm the property-raises case doesn't crash init;
    # the assertion path below uses sdk2 (plain-missing attribute).
    GovernanceCallbackHandler(
        sdk=sdk, agent_id="no-cost-agent", session_id=uuid4(),
    )

    # getattr with default on a property that raises → still raises;
    # handler uses getattr(self._sdk, "cost", None) which works when
    # the attribute is plain-missing. Force a plain missing attribute:
    sdk2 = MagicMock(spec=["audit"])
    handler2 = GovernanceCallbackHandler(
        sdk=sdk2, agent_id="no-cost-agent", session_id=uuid4(),
    )
    # Should not raise; defaults to fallback=0.0 for unknown models.
    usd = handler2._estimate_cost_safe("my-ft-gpt4-v7", 100, 50)
    assert usd == 0.0
