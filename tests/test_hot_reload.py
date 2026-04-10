"""Tests for the policy hot-reload feature."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from codeatelier_governance import GovernanceSDK


@pytest.mark.asyncio
async def test_hot_reload_task_starts_and_cancels() -> None:
    """hot_reload=True should start a background task, cancelled on close."""
    sdk = GovernanceSDK(
        database_url="postgresql://fake:fake@localhost/fake",
        hot_reload=True,
        hot_reload_interval=1,
    )
    # Mock out the poll to avoid real DB calls
    sdk._poll_policies = AsyncMock()  # type: ignore[assignment]
    # Also mock audit start to avoid real DB connection
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    # Mock loop/presence close
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    await sdk.start()
    assert sdk._hot_reload_task is not None
    assert not sdk._hot_reload_task.done()

    await sdk.close()
    assert sdk._hot_reload_task is None


@pytest.mark.asyncio
async def test_hot_reload_polls_policies() -> None:
    """Hot reload should call _poll_policies on interval."""
    sdk = GovernanceSDK(
        database_url="postgresql://fake:fake@localhost/fake",
        hot_reload=True,
        hot_reload_interval=1,
    )
    call_count = 0

    async def mock_poll() -> None:
        nonlocal call_count
        call_count += 1

    sdk._poll_policies = mock_poll  # type: ignore[assignment]
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    await sdk.start()
    # Wait enough time for at least one poll
    await asyncio.sleep(1.5)
    await sdk.close()
    assert call_count >= 1


@pytest.mark.asyncio
async def test_hot_reload_error_does_not_crash() -> None:
    """Errors in hot-reload polling should not crash the task."""
    sdk = GovernanceSDK(
        database_url="postgresql://fake:fake@localhost/fake",
        hot_reload=True,
        hot_reload_interval=1,
    )
    call_count = 0

    async def mock_poll_with_error() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated DB error")

    sdk._poll_policies = mock_poll_with_error  # type: ignore[assignment]
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    await sdk.start()
    await asyncio.sleep(2.5)
    await sdk.close()
    # Should have been called at least twice (first errored, second succeeded)
    assert call_count >= 2
