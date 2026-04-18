"""Tests for built-in model pricing (G3)."""
from __future__ import annotations

from uuid import uuid4

import pytest

from codeatelier_governance.cost.pricing import MODEL_PRICING, estimate_cost


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------
def test_known_model_returns_correct_price() -> None:
    """gpt-4o: $2.50/1M input, $10.00/1M output."""
    cost = estimate_cost("gpt-4o", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(12.50)


def test_claude_opus_pricing() -> None:
    """claude-opus-4-6: $15/1M input, $75/1M output."""
    cost = estimate_cost("claude-opus-4-6", input_tokens=1000, output_tokens=500)
    expected = (1000 * 15.0 + 500 * 75.0) / 1_000_000
    assert cost == pytest.approx(expected)


def test_unknown_model_raises_by_default() -> None:
    """v0.6.2 Bug #7: unknown model must raise, not silently return 0."""
    from codeatelier_governance.cost.errors import UnknownModelError

    with pytest.raises(UnknownModelError, match="No pricing entry"):
        estimate_cost("totally-unknown-model", 1000, 1000)


def test_unknown_model_lax_mode_returns_zero_with_warning() -> None:
    """strict=False with no fallback behaves like the old default (0 + warning)."""
    import structlog.testing

    with structlog.testing.capture_logs() as cap:
        out = estimate_cost("totally-unknown-model", 1000, 1000, strict=False)
    assert out == 0.0
    assert any(e.get("event") == "cost.unknown_model" for e in cap)


def test_unknown_model_lax_mode_applies_fallback_rate() -> None:
    """strict=False + fallback rate: apply the rate uniformly to tokens."""
    out = estimate_cost(
        "my-ft-gpt4",
        input_tokens=1_000_000,
        output_tokens=0,
        strict=False,
        fallback_usd_per_million=5.0,
    )
    assert out == pytest.approx(5.0)


def test_prefix_matching_for_versioned_model() -> None:
    """gpt-4o-2024-05-13 should match gpt-4o entry."""
    cost = estimate_cost("gpt-4o-2024-05-13", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(2.50)


def test_prefix_matching_prefers_longer_prefix() -> None:
    """gpt-4o-mini should match gpt-4o-mini, not gpt-4o."""
    cost = estimate_cost("gpt-4o-mini", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(0.15)


def test_zero_tokens_returns_zero() -> None:
    assert estimate_cost("gpt-4o", input_tokens=0, output_tokens=0) == 0.0


def test_all_models_have_input_and_output_keys() -> None:
    """Every model in the pricing table must have both input and output."""
    for model, pricing in MODEL_PRICING.items():
        assert "input" in pricing, f"{model} missing 'input'"
        assert "output" in pricing, f"{model} missing 'output'"
        assert pricing["input"] >= 0, f"{model} has negative input price"
        assert pricing["output"] >= 0, f"{model} has negative output price"


# ---------------------------------------------------------------------------
# track_usage integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_track_usage_calls_track_with_correct_usd(cost):  # type: ignore[no-untyped-def]
    """track_usage should compute USD from pricing and delegate to track."""
    from codeatelier_governance.cost import BudgetPolicy

    cost.register(BudgetPolicy(agent_id="a", per_session_usd=100.0))
    sid = uuid4()
    await cost.track_usage(
        "a", sid, model="gpt-4o", input_tokens=1000, output_tokens=500
    )
    snap = await cost.snapshot("a", sid)
    expected_usd = (1000 * 2.50 + 500 * 10.00) / 1_000_000
    assert snap.session_usd_used == pytest.approx(expected_usd)
    assert snap.session_tokens_used == 1500


@pytest.mark.asyncio
async def test_track_usage_unknown_model_raises_by_default(cost):  # type: ignore[no-untyped-def]
    """v0.6.2: unknown model in strict mode raises, does NOT track silently."""
    from codeatelier_governance.cost import BudgetPolicy
    from codeatelier_governance.cost.errors import UnknownModelError

    cost.register(BudgetPolicy(agent_id="a", per_session_tokens=10000))
    sid = uuid4()
    with pytest.raises(UnknownModelError):
        await cost.track_usage(
            "a", sid, model="unknown-model", input_tokens=100, output_tokens=50
        )
    snap = await cost.snapshot("a", sid)
    # No tokens tracked because the call failed loudly.
    assert snap.session_tokens_used == 0


@pytest.mark.asyncio
async def test_track_usage_unknown_model_lax_mode_tracks_tokens(  # type: ignore[no-untyped-def]
    audit,
):
    """Lax mode (strict_unknown_models=False): tokens still tracked, usd uses fallback."""
    from codeatelier_governance.cost import BudgetPolicy, CostModule

    lax_cost = CostModule(
        audit,
        strict_unknown_models=False,
        unknown_model_fallback_usd_per_million=2.0,
    )
    lax_cost.register(BudgetPolicy(agent_id="a", per_session_tokens=10000))
    sid = uuid4()
    await lax_cost.track_usage(
        "a", sid, model="unknown-model", input_tokens=100, output_tokens=50
    )
    snap = await lax_cost.snapshot("a", sid)
    assert snap.session_tokens_used == 150
    # usd = (100 + 50) * 2.0 / 1_000_000
    assert snap.session_usd_used == pytest.approx(150 * 2.0 / 1_000_000)
