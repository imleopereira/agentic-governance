"""Unit tests for CoverageComputer."""
from __future__ import annotations

import pytest

from codeatelier_governance.coverage.compute import CoverageComputer


@pytest.mark.asyncio
async def test_disabled_when_engine_none() -> None:
    comp = CoverageComputer(engine=None, enabled=True)
    pct, reason = await comp.compute()
    assert pct is None
    assert reason == "registry_disabled"


@pytest.mark.asyncio
async def test_disabled_when_enabled_false() -> None:
    # Use a sentinel object — compute() must short-circuit before touching it.
    comp = CoverageComputer(engine=object(), enabled=False)
    pct, reason = await comp.compute()
    assert pct is None
    assert reason == "registry_disabled"


@pytest.mark.asyncio
async def test_swallows_exceptions_and_returns_disabled() -> None:
    class BrokenEngine:
        def connect(self) -> "BrokenEngine":
            raise RuntimeError("DB down")

    comp = CoverageComputer(engine=BrokenEngine(), enabled=True)
    pct, reason = await comp.compute()
    assert pct is None
    assert reason == "registry_disabled"


def test_default_active_window_is_seven_days() -> None:
    comp = CoverageComputer(engine=None)
    assert comp._active_window_days == 7
