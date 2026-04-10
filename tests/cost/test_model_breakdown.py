"""Tests for per-model cost aggregation."""
from __future__ import annotations

from uuid import uuid4

import pytest

from codeatelier_governance.cost import CostModule


@pytest.mark.asyncio
async def test_model_breakdown_two_models(cost: CostModule) -> None:
    """Track two different models and verify breakdown."""
    sid = uuid4()
    await cost.track("a", sid, tokens=100, usd=0.10, model="gpt-4o")
    await cost.track("a", sid, tokens=200, usd=0.05, model="gpt-4o-mini")
    await cost.track("a", sid, tokens=50, usd=0.08, model="gpt-4o")

    breakdown = await cost.model_breakdown("a")
    assert "gpt-4o" in breakdown
    assert "gpt-4o-mini" in breakdown
    assert breakdown["gpt-4o"]["usd"] == pytest.approx(0.18)
    assert breakdown["gpt-4o"]["tokens"] == pytest.approx(150.0)
    assert breakdown["gpt-4o-mini"]["usd"] == pytest.approx(0.05)
    assert breakdown["gpt-4o-mini"]["tokens"] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_model_breakdown_empty(cost: CostModule) -> None:
    """model_breakdown with no data should return empty dict."""
    breakdown = await cost.model_breakdown("nonexistent")
    assert breakdown == {}


@pytest.mark.asyncio
async def test_track_without_model_no_breakdown(cost: CostModule) -> None:
    """track() without model should not appear in model_breakdown."""
    sid = uuid4()
    await cost.track("a", sid, tokens=100, usd=0.10)
    breakdown = await cost.model_breakdown("a")
    assert breakdown == {}


@pytest.mark.asyncio
async def test_track_usage_passes_model(cost: CostModule) -> None:
    """track_usage() should pass model through to track()."""
    sid = uuid4()
    await cost.track_usage(
        "a", sid, model="gpt-4o", input_tokens=1000, output_tokens=500,
    )
    breakdown = await cost.model_breakdown("a")
    assert "gpt-4o" in breakdown
    assert breakdown["gpt-4o"]["tokens"] == pytest.approx(1500.0)
