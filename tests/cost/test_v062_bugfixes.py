"""Behavior tests for v0.6.2 Track C cost fixes (Bugs #4 + #7).

These tests MUST fail against v0.6.1 and pass against the v0.6.2 fix.
No source-grep asserts — every test exercises real ``estimate_cost`` /
``CostModule.track_usage`` / ``check_or_raise`` behavior.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
import structlog.testing

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.cost import (
    BudgetExceeded,
    BudgetPolicy,
    CostModule,
)
from codeatelier_governance.cost.errors import UnknownModelError
from codeatelier_governance.cost.pricing import MODEL_PRICING, estimate_cost


# ---------------------------------------------------------------------------
# Bug #4 — claude-opus-4-7 pricing
# ---------------------------------------------------------------------------


def test_bug4_opus_4_7_is_in_pricing_table() -> None:
    """claude-opus-4-7 must be priced, not silently 0."""
    assert "claude-opus-4-7" in MODEL_PRICING
    pricing = MODEL_PRICING["claude-opus-4-7"]
    assert pricing["input"] > 0.0
    assert pricing["output"] > 0.0


def test_bug4_opus_4_7_estimate_cost_is_nonzero() -> None:
    """estimate_cost('claude-opus-4-7', ...) must be > 0 for non-zero tokens."""
    cost = estimate_cost(
        "claude-opus-4-7", input_tokens=1_000_000, output_tokens=1_000_000,
    )
    assert cost > 0.0


def test_bug4_opus_4_7_rate_at_least_equal_to_4_6() -> None:
    """Opus 4.7 rate must be >= Opus 4.6 (Anthropic hasn't historically reduced opus pricing)."""
    cost_4_7 = estimate_cost("claude-opus-4-7", input_tokens=1_000_000, output_tokens=0)
    cost_4_6 = estimate_cost("claude-opus-4-6", input_tokens=1_000_000, output_tokens=0)
    assert cost_4_7 >= cost_4_6


def test_bug4_opus_4_7_dated_variant_prefix_matches() -> None:
    """claude-opus-4-7-YYYYMMDD should match claude-opus-4-7 via prefix."""
    dated_cost = estimate_cost(
        "claude-opus-4-7-20260415",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    base_cost = estimate_cost(
        "claude-opus-4-7",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    assert dated_cost == pytest.approx(base_cost)


@pytest_asyncio.fixture
async def audit_store_local() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit_local(audit_store_local: InMemoryAuditStore) -> AuditModule:
    import secrets as _secrets

    writer = BatchingWriter(
        primary=audit_store_local,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(
        audit_store_local, secret=_secrets.token_bytes(32), writer=writer,
    )
    await module.start()
    try:
        yield module  # type: ignore[misc]
    finally:
        await module.close()


@pytest.mark.asyncio
async def test_bug4_opus_4_7_trips_session_usd_budget(audit_local: AuditModule) -> None:
    """A session_usd cap must be tripped by enough Opus 4.7 tokens."""
    # With $15/1M input and $75/1M output: 100k input + 100k output = $1.50 + $7.50 = $9.00
    # A cap of $5 should trip.
    cost_module = CostModule(audit_local)
    cost_module.register(BudgetPolicy(agent_id="opus47", per_session_usd=5.0))
    sid = uuid4()
    await cost_module.track_usage(
        "opus47",
        sid,
        model="claude-opus-4-7",
        input_tokens=100_000,
        output_tokens=100_000,
    )
    with pytest.raises(BudgetExceeded, match="per_session_usd"):
        await cost_module.check_or_raise("opus47", sid)


# ---------------------------------------------------------------------------
# Bug #7 — Unknown model no longer silently returns 0
# ---------------------------------------------------------------------------


def test_bug7_strict_mode_unknown_model_raises() -> None:
    """Default strict mode: estimate_cost on unknown model → UnknownModelError."""
    with pytest.raises(UnknownModelError, match="my-ft-gpt4"):
        estimate_cost("my-ft-gpt4", input_tokens=1000, output_tokens=1000)


def test_bug7_lax_mode_emits_structlog_warning() -> None:
    """Lax mode must emit cost.unknown_model warning via structlog."""
    with structlog.testing.capture_logs() as cap:
        estimate_cost(
            "my-ft-gpt4",
            input_tokens=1000,
            output_tokens=1000,
            strict=False,
        )
    warnings = [e for e in cap if e.get("event") == "cost.unknown_model"]
    assert len(warnings) == 1
    assert warnings[0]["model"] == "my-ft-gpt4"


def test_bug7_lax_mode_with_fallback_applies_rate() -> None:
    """Lax mode + fallback_usd_per_million: unknown model gets the fallback rate."""
    out = estimate_cost(
        "my-ft-gpt4",
        input_tokens=500_000,
        output_tokens=500_000,
        strict=False,
        fallback_usd_per_million=4.0,
    )
    # 1_000_000 total tokens * 4.0 / 1_000_000 = 4.0
    assert out == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_bug7_cost_module_strict_default_true_raises(
    audit_local: AuditModule,
) -> None:
    """CostModule.strict_unknown_models defaults True in v0.6.2 — unknown model raises."""
    cost_module = CostModule(audit_local)
    cost_module.register(BudgetPolicy(agent_id="ft", per_session_tokens=10000))
    sid = uuid4()
    with pytest.raises(UnknownModelError):
        await cost_module.track_usage(
            "ft",
            sid,
            model="my-ft-gpt4",
            input_tokens=500,
            output_tokens=200,
        )


@pytest.mark.asyncio
async def test_bug7_lax_cost_module_does_not_bypass_budget_via_fallback(
    audit_local: AuditModule,
) -> None:
    """Lax mode + non-zero fallback: unknown model still accumulates USD → cap trips."""
    cost_module = CostModule(
        audit_local,
        strict_unknown_models=False,
        unknown_model_fallback_usd_per_million=10.0,
    )
    cost_module.register(BudgetPolicy(agent_id="ft", per_session_usd=0.005))
    sid = uuid4()
    # 1000 tokens * 10.0 / 1M = 0.01 USD, exceeds 0.005 cap.
    await cost_module.track_usage(
        "ft",
        sid,
        model="my-ft-gpt4",
        input_tokens=500,
        output_tokens=500,
    )
    with pytest.raises(BudgetExceeded, match="per_session_usd"):
        await cost_module.check_or_raise("ft", sid)


@pytest.mark.asyncio
async def test_bug7_lax_cost_module_with_zero_fallback_is_documented_footgun(
    audit_local: AuditModule,
) -> None:
    """Lax + no fallback = old foot-gun behavior; warning must fire for visibility."""
    cost_module = CostModule(
        audit_local,
        strict_unknown_models=False,
        unknown_model_fallback_usd_per_million=None,  # → 0.0
    )
    cost_module.register(BudgetPolicy(agent_id="ft", per_session_usd=0.01))
    sid = uuid4()
    with structlog.testing.capture_logs() as cap:
        await cost_module.track_usage(
            "ft",
            sid,
            model="my-ft-gpt4",
            input_tokens=500,
            output_tokens=500,
        )
    # No USD tracked (fallback is 0) — explicit foot-gun, but warning proves
    # the path was traversed so silent-zero is no longer silent.
    assert any(e.get("event") == "cost.unknown_model" for e in cap)
    # And the budget check should still pass (documented lax behavior).
    await cost_module.check_or_raise("ft", sid)
