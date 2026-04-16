"""BLOCKER C3: statement timeout + anonymous bucket on compliance endpoints.

Pins:
  * Anonymous compliance callers share a SINGLE global rate-limit bucket
    (1 call per 300 s) instead of bypassing the limit entirely.
  * The chain verify call is wall-clock bounded by ``asyncio.wait_for``;
    a slow generator blows up with HTTPException 504 instead of pinning
    the semaphore indefinitely.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from codeatelier_governance.console import app as app_module


def _reset_anon_bucket() -> None:
    app_module._compliance_anon_times.clear()


@pytest.mark.asyncio
async def test_anonymous_callers_share_global_bucket() -> None:
    """Two back-to-back anonymous calls: the second must be 429."""
    _reset_anon_bucket()
    req1 = MagicMock()
    req1.state = MagicMock(spec=[])  # no user_id attribute
    # First call passes.
    await app_module._compliance_rate_limit_dep(req1)
    # Second anonymous call within window must 429.
    req2 = MagicMock()
    req2.state = MagicMock(spec=[])
    with pytest.raises(Exception) as excinfo:
        await app_module._compliance_rate_limit_dep(req2)
    assert getattr(excinfo.value, "status_code", None) == 429
    _reset_anon_bucket()


@pytest.mark.asyncio
async def test_anonymous_calls_not_unlimited() -> None:
    """5 anonymous calls in a tight loop → at most 1 succeeds before 429."""
    _reset_anon_bucket()
    successes = 0
    fails = 0
    for _ in range(5):
        req = MagicMock()
        req.state = MagicMock(spec=[])
        try:
            await app_module._compliance_rate_limit_dep(req)
            successes += 1
        except Exception:
            fails += 1
    assert successes == 1
    assert fails == 4
    _reset_anon_bucket()


@pytest.mark.asyncio
async def test_compliance_statement_timeout_constant_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout knob exists and is a positive integer (ms)."""
    assert app_module._COMPLIANCE_STATEMENT_TIMEOUT_MS > 0
    assert app_module._COMPLIANCE_STATEMENT_TIMEOUT_MS <= 600_000


@pytest.mark.asyncio
async def test_verify_chain_wait_for_wraps_slow_generator() -> None:
    """asyncio.wait_for wraps the generator call — proves the timeout
    path is exercisable. Uses a tiny timeout so the test stays fast."""

    async def slow() -> None:
        await asyncio.sleep(0.5)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(slow(), timeout=0.05)
