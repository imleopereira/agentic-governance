"""Unit tests for the v0.7.0 platform bridge — mocked HTTP only.

These tests exercise :class:`PlatformClient` end-to-end against mocked
responses using ``respx``. They live in the SDK's committed ``tests/``
tree so every PR runs them in CI without needing a live platform stack.

Coverage focus (see task brief):
    1. Happy path → ``stats.sent`` increments.
    2. 401 → bridge latches ``disabled`` after drain.
    3. 429 honors ``Retry-After`` and eventually sends.
    4. 413 drops with ``dropped_4xx`` bump.
    5. 409 ``seq_out_of_order`` self-heals the next POST's ``seq``.
    6. Continuous 503 → retry budget exhaustion → ``dropped_5xx >= 1``.
    7. Queue-full drops oldest AND rate-limits WARN to one per minute.
    8. Post-heal 409 increments ``dropped_seq_conflict``.
    9. ``trusted_hosts`` allowlist rejects unknown host at init.
    10. Body preview scrubs ``Bearer <token>`` before logging.

The live dual-write / server-side assertions (platform really ingested
the event, DB row exists, etc.) remain in ``tests-internal/`` — those
require a running platform and are being moved to the platform repo.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from codeatelier_governance.audit.models import AuditEventRecord
from codeatelier_governance.platform.client import PlatformClient
from codeatelier_governance.platform.config import PlatformConfig
from codeatelier_governance.platform.errors import PlatformConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_URL = "https://platform.codeatelier.test/api/v1/ingest/events"
_TEST_TOKEN = "wf_test_abcdef0123456789"  # pragma: allowlist secret


def _make_record(kind: str = "tool.call") -> AuditEventRecord:
    """Minimal AuditEventRecord with all required chain fields."""
    return AuditEventRecord(
        event_id=uuid4(),
        session_id=uuid4(),
        agent_id="unit-test-agent",
        parent_event_id=None,
        kind=kind,
        model=None,
        input_hash=None,
        output_hash=None,
        metadata={"probe": "unit"},
        prev_hash=None,
        hmac="a" * 64,
        created_at=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
    )


def _make_config(
    *,
    max_retries: int = 4,
    max_queue_size: int = 1000,
    trusted_hosts: tuple[str, ...] | None = None,
    url: str = _TEST_URL,
) -> PlatformConfig:
    return PlatformConfig(
        ingest_url=url,
        ingest_token=_TEST_TOKEN,
        enabled=True,
        max_queue_size=max_queue_size,
        max_retries=max_retries,
        timeout_seconds=2.0,
        trusted_hosts=trusted_hosts,
    )


async def _drain(client: PlatformClient, *, timeout: float = 5.0) -> None:
    """Wait for the queue to drain or raise TimeoutError."""
    await asyncio.wait_for(client._queue.join(), timeout=timeout)


async def _wait_stats(
    client: PlatformClient,
    predicate: Any,
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
) -> dict[str, Any]:
    """Poll stats() until predicate(stats) is truthy or timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    stats: dict[str, Any] = client.stats()
    while loop.time() < deadline:
        stats = client.stats()
        if predicate(stats):
            return stats
        await asyncio.sleep(interval)
    return stats


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_happy_path_increments_sent() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_TEST_URL).respond(200, json={"ok": True})
        client = PlatformClient(_make_config())
        await client.start()
        try:
            client.forward(_make_record())
            await _drain(client)
        finally:
            await client.close()
        assert route.called
        stats = client.stats()
        assert stats["sent"] >= 1
        assert stats["dropped_4xx"] == 0
        assert stats["dropped_5xx"] == 0


# ---------------------------------------------------------------------------
# 2. 401 latches bridge disabled
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_401_latches_bridge_disabled() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_TEST_URL).respond(401, json={"error": "invalid_token"})
        client = PlatformClient(_make_config())
        await client.start()
        try:
            client.forward(_make_record())
            stats = await _wait_stats(
                client, lambda s: s["disabled"] is True
            )
        finally:
            await client.close()
        assert stats["disabled"] is True
        assert stats["dropped_4xx"] >= 1


# ---------------------------------------------------------------------------
# 3. 429 honors Retry-After
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_429_honors_retry_after() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        responses = [
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "rate_limited"}),
            httpx.Response(200, json={"ok": True}),
        ]
        mock.post(_TEST_URL).mock(side_effect=responses)
        client = PlatformClient(_make_config())
        await client.start()
        try:
            client.forward(_make_record())
            await _drain(client, timeout=10.0)
        finally:
            await client.close()
        stats = client.stats()
        assert stats["retries"] >= 1
        assert stats["sent"] >= 1


# ---------------------------------------------------------------------------
# 4. 413 drops with metric
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_413_drops_with_metric() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_TEST_URL).respond(413, json={"error": "payload_too_large"})
        client = PlatformClient(_make_config())
        await client.start()
        try:
            client.forward(_make_record())
            stats = await _wait_stats(
                client, lambda s: s["dropped_4xx"] >= 1
            )
        finally:
            await client.close()
        assert stats["dropped_4xx"] >= 1
        assert stats["sent"] == 0


# ---------------------------------------------------------------------------
# 5. 409 seq_out_of_order self-heal
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_409_seq_self_heal_parses_expected_seq() -> None:
    captured: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        # First call: tell the SDK its seq is wrong and what to use.
        if len(captured) == 1:
            return httpx.Response(
                409,
                json={
                    "ok": False,
                    "error": "seq_out_of_order",
                    "message": "expected seq=42, got 1",
                },
            )
        # Subsequent calls: accept.
        return httpx.Response(200, json={"ok": True})

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_TEST_URL).mock(side_effect=_capture)
        client = PlatformClient(_make_config())
        await client.start()
        try:
            client.forward(_make_record())
            await _drain(client, timeout=5.0)
        finally:
            await client.close()

    # Two POSTs: first with seq=1, retry with seq=42 (self-healed).
    assert len(captured) >= 2, f"expected two POSTs; got {len(captured)}"
    assert captured[0]["seq"] == 1
    assert captured[1]["seq"] == 42
    stats = client.stats()
    assert stats["sent"] >= 1
    # Self-heal path ≠ conflict-drop path. This is the GOOD retry.
    assert stats["dropped_seq_conflict"] == 0


# ---------------------------------------------------------------------------
# 6. Retry budget exhaustion on continuous 503
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_503_retry_budget_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the client's ``_sleep_with_cap`` directly so the 15s budget
    elapses deterministically in ms, without touching global asyncio.sleep
    (which would recurse through _wait_stats). Simulates "every retry
    burned 4s of wall-clock" so we trip the 15s cap after 3-4 attempts.
    """
    import time as time_mod

    import codeatelier_governance.platform.client as client_mod

    t0: float = time_mod.monotonic()
    fake_elapsed: list[float] = [0.0]

    async def _instant_sleep_that_advances_clock(
        self: PlatformClient, delay: float, start: float
    ) -> bool:
        # Mirror the real cap check but advance a virtual clock so the
        # per-event budget expires after a few retries.
        fake_elapsed[0] += max(delay, 4.0)
        remaining = client_mod.MAX_CUMULATIVE_RETRY_SECONDS - fake_elapsed[0]
        if remaining <= 0:
            return False
        # Yield so the worker loop can advance.
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(
        client_mod.PlatformClient, "_sleep_with_cap",
        _instant_sleep_that_advances_clock,
    )

    # Also freeze start-relative timing: when _forward_one computes
    # ``elapsed = time.monotonic() - start``, the check happens BEFORE
    # the sleep. Patch ``time.monotonic`` in the client module's
    # namespace so it returns start + fake_elapsed[0].
    start_ref: list[float] = [t0]

    def _fake_monotonic() -> float:
        return start_ref[0] + fake_elapsed[0]

    # client.py does ``import time`` and reads ``time.monotonic`` at
    # call-sites; patch the module-object to which client_mod.time
    # refers. Using setattr on the time module would be globally
    # destructive; we instead swap the binding on the client module.
    monkeypatch.setattr(
        client_mod, "time", type("_FakeTime", (), {"monotonic": staticmethod(_fake_monotonic)})
    )

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_TEST_URL).respond(503, json={"error": "service_unavailable"})
        client = PlatformClient(_make_config(max_retries=10))
        await client.start()
        try:
            client.forward(_make_record())
            # Yield to the worker; _sleep_with_cap's fake-clock advances
            # per retry. A short poll window (2s real-time) is plenty
            # because every yield is instant.
            await _wait_stats(
                client, lambda s: s["dropped_5xx"] >= 1, timeout=2.0
            )
        finally:
            await client.close()
        # Re-read AFTER close: the drop may be registered during the
        # worker's final iteration, and close() awaits that.
        final_stats = client.stats()
    assert final_stats["dropped_5xx"] >= 1, (
        f"expected budget-exhaustion drop; stats={final_stats!r}"
    )
    assert final_stats["retries"] >= 1


# ---------------------------------------------------------------------------
# 7. Queue-full drops oldest + rate-limits WARN
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_queue_full_drops_oldest_and_rate_limits_warn() -> None:
    # Don't start the worker — that way forward() puts into a queue
    # nothing is draining from, and we can deterministically fill it.
    client = PlatformClient(_make_config(max_queue_size=2))
    # Manually mark as started so forward() doesn't fast-drop on the
    # not-started branch. We intentionally skip await client.start() so
    # no worker runs.
    client._started = True
    with capture_logs() as captured:
        try:
            # Fill the queue.
            for _ in range(2):
                client.forward(_make_record())
            # Now burst 1000 more — each triggers drop-oldest.
            for _ in range(1000):
                client.forward(_make_record())
        finally:
            client._started = False
            # Don't call close() — we never started the worker, no httpx
            # client was created.
    stats = client.stats()
    # 1000 bursted events each caused an oldest-drop.
    assert stats["dropped_queue_full"] >= 1000

    # WARN rate-limiting: expect EXACTLY ONE summary event (the first
    # drop in the current 60s window). Without the SWE-B P1 fix, this
    # would be ~1000 per-event WARN lines.
    summaries = [
        e for e in captured
        if e.get("event") == "platform.queue_full_drop_summary"
    ]
    assert len(summaries) == 1, (
        f"expected exactly 1 queue-full summary WARN; got {len(summaries)} "
        f"(events={[e.get('event') for e in captured]!r})"
    )
    # And it must carry the coalesced count.
    assert summaries[0]["dropped_since_last_warn"] >= 1
    assert summaries[0]["log_level"] == "warning"


# ---------------------------------------------------------------------------
# 8. Seq-conflict silent-drop counter increments on post-heal 409
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_seq_conflict_silent_drop_counter() -> None:
    """Two 409s on the same event:

    - First: carries ``expected_seq=42`` → triggers single-shot self-heal.
    - Second: still 409 (parallel writer raced ahead) → no more self-heal
      allowed; must land in ``dropped_seq_conflict``.
    """
    call_count = [0]

    def _always_409(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error": "seq_out_of_order",
                "message": f"expected seq={50 + call_count[0]}, got {call_count[0]}",
            },
        )

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_TEST_URL).mock(side_effect=_always_409)
        client = PlatformClient(_make_config(max_retries=4))
        await client.start()
        try:
            client.forward(_make_record())
            stats = await _wait_stats(
                client, lambda s: s["dropped_seq_conflict"] >= 1, timeout=5.0
            )
        finally:
            await client.close()
    assert stats["dropped_seq_conflict"] >= 1
    assert call_count[0] >= 2  # one original + at least one self-heal retry


# ---------------------------------------------------------------------------
# 9. trusted_hosts allowlist rejects at init
# ---------------------------------------------------------------------------
def test_trusted_hosts_allowlist_rejects() -> None:
    config = _make_config(
        url="https://attacker.example/api/v1/ingest/events",
        trusted_hosts=("platform.codeatelier.tech",),
    )
    with pytest.raises(PlatformConfigError, match="trusted_hosts"):
        PlatformClient(config)


def test_trusted_hosts_allowlist_allows() -> None:
    # Sanity: matching host passes.
    config = _make_config(
        url="https://platform.codeatelier.tech/api/v1/ingest/events",
        trusted_hosts=("platform.codeatelier.tech",),
    )
    # Should not raise.
    client = PlatformClient(config)
    assert client is not None


# ---------------------------------------------------------------------------
# 10. Body-preview scrubs Bearer token
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_body_preview_scrubs_bearer_token() -> None:
    # 403 (not 401/409/413) routes through the generic 4xx WARN that
    # embeds body_preview in the log event.
    leaked_token = f"Bearer {_TEST_TOKEN}"
    body_text = (
        f"<html>reverse proxy echoed auth header: {leaked_token} "
        f"and some other text</html>"
    )
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_TEST_URL).respond(
            403,
            text=body_text,
        )
        client = PlatformClient(_make_config())
        await client.start()
        with capture_logs() as captured:
            try:
                client.forward(_make_record())
                await _wait_stats(client, lambda s: s["dropped_4xx"] >= 1)
            finally:
                await client.close()

    # The plaintext token MUST NOT appear in any structured value.
    # _TEST_TOKEN is distinctive enough that a single substring scan
    # across every string value is sufficient.
    def _walk(obj: Any) -> None:
        if isinstance(obj, str):
            assert _TEST_TOKEN not in obj, (
                f"bearer token leaked to structlog value: {obj!r}"
            )
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                _walk(v)

    for event in captured:
        _walk(event)

    # Confirm we DID hit the 4xx branch — otherwise the test passes
    # trivially when nothing happens.
    assert any(
        e.get("event") == "platform.client_error" for e in captured
    ), f"expected platform.client_error event; got {[e.get('event') for e in captured]!r}"
    # And the body_preview MUST be the REDACTED form.
    err_events = [
        e for e in captured if e.get("event") == "platform.client_error"
    ]
    assert err_events
    assert "Bearer <redacted>" in err_events[0]["body_preview"]
