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
    polled = asyncio.Event()

    async def mock_poll() -> None:
        polled.set()

    sdk._poll_policies = mock_poll  # type: ignore[assignment]
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    # Use a very short interval so we don't wait long
    sdk._hot_reload_interval = 0.05

    await sdk.start()
    # Wait for the event to be set (poll was called) with a timeout
    await asyncio.wait_for(polled.wait(), timeout=2.0)
    await sdk.close()
    assert polled.is_set()


@pytest.mark.asyncio
async def test_hot_reload_error_does_not_crash() -> None:
    """Errors in hot-reload polling should not crash the task."""
    sdk = GovernanceSDK(
        database_url="postgresql://fake:fake@localhost/fake",
        hot_reload=True,
        hot_reload_interval=1,
    )
    call_count = 0
    second_call = asyncio.Event()

    async def mock_poll_with_error() -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated DB error")
        # Signal that we survived the error and got called again
        second_call.set()

    sdk._poll_policies = mock_poll_with_error  # type: ignore[assignment]
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    # Use a very short interval so we don't wait long
    sdk._hot_reload_interval = 0.05

    await sdk.start()
    # Wait until the second poll succeeds (proving the loop survived the error)
    await asyncio.wait_for(second_call.wait(), timeout=2.0)
    await sdk.close()
    # Should have been called at least twice (first errored, second succeeded)
    assert call_count >= 2


# -- Gap #3: Cold-start policy loading -----------------------------------------


@pytest.mark.asyncio
async def test_cold_start_loads_policies_before_background_task() -> None:
    """sdk.start() must call _poll_policies() synchronously before spawning
    the hot-reload background task. This eliminates the cold-start window
    where policies dict is empty.
    """
    sdk = GovernanceSDK(
        database_url="postgresql://fake:fake@localhost/fake",
        hot_reload=True,
        hot_reload_interval=60,  # Long interval so the background task can't sneak in
    )
    call_order: list[str] = []

    async def mock_poll() -> None:
        call_order.append("poll")

    original_start_hot_reload = sdk.start_hot_reload

    async def mock_start_hot_reload(interval_seconds: int = 30) -> None:
        call_order.append("hot_reload_start")
        await original_start_hot_reload(interval_seconds)

    sdk._poll_policies = mock_poll  # type: ignore[assignment]
    sdk.start_hot_reload = mock_start_hot_reload  # type: ignore[method-assign]
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    await sdk.start()

    # poll must happen BEFORE the hot_reload_start
    assert call_order[0] == "poll"
    assert call_order[1] == "hot_reload_start"

    await sdk.close()


@pytest.mark.asyncio
async def test_cold_start_failure_does_not_block_start() -> None:
    """If the initial _poll_policies() fails during start(), the SDK should
    still start successfully (policies will load on the first background poll).
    """
    sdk = GovernanceSDK(
        database_url="postgresql://fake:fake@localhost/fake",
        hot_reload=True,
        hot_reload_interval=60,
    )

    async def failing_poll() -> None:
        raise RuntimeError("DB unreachable on cold start")

    sdk._poll_policies = failing_poll  # type: ignore[assignment]
    sdk.audit.start = AsyncMock()  # type: ignore[method-assign]
    sdk.audit.close = AsyncMock()  # type: ignore[method-assign]
    sdk.loop.close = AsyncMock()  # type: ignore[method-assign]
    sdk.presence.close = AsyncMock()  # type: ignore[method-assign]

    # Should NOT raise despite the poll failure
    await sdk.start()
    assert sdk._started is True
    assert sdk._hot_reload_task is not None

    await sdk.close()
