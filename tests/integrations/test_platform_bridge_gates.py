"""Unit tests for the v0.7.1 platform-bridge HITL gates contract.

Coverage focus (from the v0.7.1 task brief and spec §3):

1. ``forward_gate_request`` is fire-and-forget; platform 5xx does not
   break gate creation.
2. Poll finds ``pending`` on the first tick, ``granted`` on the second
   -> local store gets synced (the on_commit audit row lands with
   ``by=platform``).
3. Local ``granted`` + platform ``pending`` -> local wins; wait_for
   returns True without syncing anything.
4. Platform ``granted`` but bridge becomes unreachable DURING polling
   -> SDK keeps polling local; gate expires at timeout with
   ``ApprovalTimeout``.
5. Jitter is actually randomised -> mock ``random.uniform`` and
   observe the jitter actually feeds the sleep window.
6. Exponential backoff on platform 5xx up to 10s cap.

All tests mock httpx via ``respx`` so CI does not need a running
platform.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.gates import (
    ApprovalDenied,
    ApprovalTimeout,
    GatesModule,
)
from codeatelier_governance.gates.models import ApprovalRequest
from codeatelier_governance.platform.client import (
    PlatformClient,
    PlatformGateResolution,
)
from codeatelier_governance.platform.config import PlatformConfig


_BASE_URL = "https://platform.codeatelier.test"
_INGEST_URL = f"{_BASE_URL}/api/v1/ingest/events"
_TEST_TOKEN = "wf_gate_abcdef0123456789"  # pragma: allowlist secret
# Need a secret with sufficient entropy — ``_check_secret_strength``
# rejects low-unique-byte placeholders like b"x" * 32. token_bytes
# gives real randomness, suitable for HMAC even in unit tests.
_SECRET = secrets.token_bytes(32)


def _make_platform_config() -> PlatformConfig:
    return PlatformConfig(
        ingest_url=_INGEST_URL,
        ingest_token=_TEST_TOKEN,
        enabled=True,
        timeout_seconds=2.0,
    )


async def _make_gates(
    platform_client: PlatformClient | None = None,
) -> tuple[GatesModule, AuditModule, InMemoryAuditStore]:
    """Build a GatesModule backed by an in-memory audit store.

    Returned audit + store so tests can assert on emitted audit rows.
    """
    audit_store = InMemoryAuditStore()
    audit = AuditModule(store=audit_store, secret=_SECRET)
    await audit.start()
    gates = GatesModule(
        audit,
        secret=_SECRET,
        platform_client=platform_client,
        poll_interval_s=0.01,  # tight loop so tests finish quickly
    )
    return gates, audit, audit_store


def _bridge_post_url(request_id: UUID) -> str:
    return f"{_BASE_URL}/api/v1/bridge/gates/{request_id}"


def _bridge_get_url(request_id: UUID) -> str:
    return f"{_BASE_URL}/api/v1/bridge/gates/{request_id}/resolution"


# ---------------------------------------------------------------------------
# 1. Fire-and-forget: platform 5xx does not break gate creation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_gate_request_fire_and_forget_on_5xx() -> None:
    """Gate creation must succeed even when the platform POST fails 503.

    The local store write is authoritative (invariant #1); the
    platform POST is a best-effort mirror whose failure must NOT
    surface to sdk.gates.request.
    """
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/.*").respond(
            503, json={"error": "service_unavailable"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _audit_store = await _make_gates(client)
        try:
            # THIS must not raise. The platform POST is spawned as a
            # background task; its eventual 503 will be swallowed.
            req = await gates.request("delete.user", "agent-A", payload={})
            assert req.request_id is not None
            # Force the background task to run to completion so stats
            # catch up. Cheapest way is a short asyncio.sleep yielding
            # to the loop.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_dropped"] >= 1:
                    break
        finally:
            await client.close()
            await gates.close()

    stats = client.stats()
    assert stats["gate_requests_dropped"] >= 1
    assert stats["gate_requests_sent"] == 0


@pytest.mark.asyncio
async def test_forward_gate_request_happy_path_increments_sent() -> None:
    async with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r".*/bridge/gates/.*").respond(
            200, json={"ok": True}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            await gates.request("delete.user", "agent-A", payload={})
            for _ in range(50):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_sent"] >= 1:
                    break
        finally:
            await client.close()
            await gates.close()

    assert route.called
    stats = client.stats()
    assert stats["gate_requests_sent"] >= 1


# ---------------------------------------------------------------------------
# 2. Poll pending->granted syncs local store with by=platform audit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_poll_pending_then_granted_syncs_local_store() -> None:
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                200, json={"status": "pending", "decided_at": None}
            )
        return httpx.Response(
            200,
            json={
                "status": "granted",
                "decided_at": "2026-04-19T12:34:56+00:00",
            },
        )

    async with respx.mock(assert_all_called=False) as mock:
        # Accept the gate-creation POST with 200.
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        # Two GETs to /resolution: first pending, second granted.
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").mock(
            side_effect=_handler
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, audit_store = await _make_gates(client)
        try:
            # Shrink the platform poll base so the test doesn't take 2s.
            import codeatelier_governance.gates.module as gates_module

            original_base = gates_module._PLATFORM_POLL_BASE_SECONDS
            gates_module._PLATFORM_POLL_BASE_SECONDS = 0.05
            try:
                req = await gates.request(
                    "delete.user", "agent-A", payload={}
                )
                granted = await gates.wait_for(
                    req.request_id, timeout=5.0
                )
            finally:
                gates_module._PLATFORM_POLL_BASE_SECONDS = original_base
        finally:
            await client.close()
            await gates.close()

    assert granted is True
    # Platform was consulted at least twice (pending then granted).
    assert call_count[0] >= 2
    # Local store now reflects the resolution AND the audit row
    # carries by=platform.
    granted_events = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert len(granted_events) == 1
    assert granted_events[0].metadata.get("by") == "platform"


# ---------------------------------------------------------------------------
# 3. Local granted + platform pending -> local wins (no sync, no ghost row)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_local_granted_platform_pending_local_wins() -> None:
    """Resolving locally via grant(token) must return True on wait_for
    even while the platform endpoint still says pending. No second
    audit row from the platform-sync path may land — that would
    produce a duplicate approval.granted for one request_id.
    """
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        # Platform always says pending in this test. The local path
        # must beat it to the verdict and not fall through to sync.
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            200, json={"status": "pending", "decided_at": None}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, audit_store = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )

            async def grant_soon() -> None:
                await asyncio.sleep(0.02)
                await gates.grant(req.token)

            asyncio.create_task(grant_soon())
            granted = await gates.wait_for(req.request_id, timeout=2.0)
        finally:
            await client.close()
            await gates.close()

    assert granted is True
    # Exactly one approval.granted row — no ghost write from the
    # platform sync path (by=platform would indicate the bug).
    granted_events = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert len(granted_events) == 1
    assert granted_events[0].metadata.get("by") != "platform"


# ---------------------------------------------------------------------------
# 4. Platform unreachable mid-poll -> gate expires at timeout
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_platform_unreachable_mid_poll_expires_at_timeout() -> None:
    """If the platform starts 500ing during polling and no one
    grants/denies the gate locally, wait_for MUST raise
    ApprovalTimeout at the user-supplied deadline — never sooner
    (invariant #1: platform is advisory only).
    """
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            500, json={"error": "internal"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            loop = asyncio.get_event_loop()
            start = loop.time()
            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.2)
            elapsed = loop.time() - start
            # Timeout is honored — not fired prematurely by the 500.
            assert elapsed >= 0.2
        finally:
            await client.close()
            await gates.close()

    # Platform poll failures are counted but no crash surfaced.
    assert client.stats()["gate_polls_failed"] >= 1


# ---------------------------------------------------------------------------
# 5. Jitter is actually random
# ---------------------------------------------------------------------------
def test_jittered_interval_uses_uniform_pm_500ms() -> None:
    """``_jittered_interval`` must return base +/- up to 500ms and
    clamp to 10s. Deterministic check: feed known uniform() outputs
    and assert the returned value.
    """
    import codeatelier_governance.gates.module as gates_module

    original_uniform = gates_module.random.uniform
    samples: list[float] = []
    try:
        # Patch random.uniform to return known values at +/- extremes.
        gates_module.random.uniform = lambda a, b: a  # type: ignore[assignment]
        samples.append(gates_module._jittered_interval(2.0))
        gates_module.random.uniform = lambda a, b: b  # type: ignore[assignment]
        samples.append(gates_module._jittered_interval(2.0))
        # Mid sample.
        gates_module.random.uniform = lambda a, b: 0.0  # type: ignore[assignment]
        samples.append(gates_module._jittered_interval(2.0))
        # Cap check: base above the 10s cap clamps.
        gates_module.random.uniform = lambda a, b: b  # type: ignore[assignment]
        samples.append(gates_module._jittered_interval(20.0))
    finally:
        gates_module.random.uniform = original_uniform  # type: ignore[assignment]

    # -500ms
    assert samples[0] == pytest.approx(1.5, abs=1e-9)
    # +500ms
    assert samples[1] == pytest.approx(2.5, abs=1e-9)
    # No jitter
    assert samples[2] == pytest.approx(2.0, abs=1e-9)
    # Clamped to 10s cap
    assert samples[3] == pytest.approx(10.0, abs=1e-9)


@pytest.mark.asyncio
async def test_wait_for_platform_poll_uses_jittered_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observe that wait_for pulls the jittered interval when
    scheduling the next platform poll — not a hardcoded constant.
    Patches ``_jittered_interval`` to a spy and verifies at least one
    call lands during a realistic polling run.
    """
    import codeatelier_governance.gates.module as gates_module

    calls: list[float] = []

    real_jitter = gates_module._jittered_interval

    def _spy(base: float) -> float:
        calls.append(base)
        return real_jitter(base)

    monkeypatch.setattr(gates_module, "_jittered_interval", _spy)

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            200, json={"status": "pending", "decided_at": None}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            # Small base so the test completes in < 1s.
            monkeypatch.setattr(
                gates_module, "_PLATFORM_POLL_BASE_SECONDS", 0.05
            )
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.25)
        finally:
            await client.close()
            await gates.close()

    # At least one platform poll should have scheduled the next poll
    # via _jittered_interval.
    assert len(calls) >= 1
    assert all(c == pytest.approx(0.05, abs=1e-9) for c in calls[:1])


# ---------------------------------------------------------------------------
# 6. Exponential backoff on platform 5xx up to 10s cap
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_exponential_backoff_on_platform_errors_doubles_up_to_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After N consecutive 5xx responses, the SDK's platform-poll
    backoff ladder must double each time, capped at
    ``_PLATFORM_POLL_MAX_SECONDS`` (10s). Verify by observing the
    bases passed into ``_jittered_interval`` after repeated failures.
    """
    import codeatelier_governance.gates.module as gates_module

    bases: list[float] = []

    def _spy(base: float) -> float:
        bases.append(base)
        # Return 0 so the test does not actually wait 2s/4s/etc.
        return 0.0

    monkeypatch.setattr(gates_module, "_jittered_interval", _spy)

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            500, json={"error": "internal"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            # Use a tiny base so the ladder clears quickly. Cap should
            # still take effect at 10s regardless of base because the
            # cap is clamped by _PLATFORM_POLL_MAX_SECONDS.
            monkeypatch.setattr(
                gates_module, "_PLATFORM_POLL_BASE_SECONDS", 2.0
            )
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            with pytest.raises(ApprovalTimeout):
                # 0.5s timeout + instant jitter should allow ~many
                # consecutive 500 errors to stack.
                await gates.wait_for(req.request_id, timeout=0.5)
        finally:
            await client.close()
            await gates.close()

    # Must have accumulated several polls with doubling bases.
    assert len(bases) >= 3, f"too few poll schedules: {bases!r}"
    # First error doubles the 2.0 base to 4.0 BEFORE scheduling the
    # next poll, so bases[0] is 4.0, bases[1] is 8.0, and from then
    # on values are clamped at the 10.0 cap.
    assert bases[0] == pytest.approx(4.0, abs=1e-9)
    # Later values must never exceed the cap.
    assert all(b <= 10.0 + 1e-9 for b in bases)
    # Doubling actually happened — at least one later value is > first.
    assert any(b > bases[0] + 1e-9 for b in bases)
    # Cap is reached and sustained.
    assert bases[-1] == pytest.approx(10.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 7. poll_gate_resolution parses the platform wire shape defensively
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_poll_gate_resolution_parses_all_statuses() -> None:
    request_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    for status_str, expected in (
        ("pending", "pending"),
        ("granted", "granted"),
        ("denied", "denied"),
        # Unknown status -> pending (safe default)
        ("weird", "pending"),
    ):
        async with respx.mock(assert_all_called=False) as mock:
            mock.get(_bridge_get_url(request_id)).respond(
                200,
                json={
                    "status": status_str,
                    "decided_at": "2026-04-19T12:34:56+00:00",
                },
            )
            client = PlatformClient(_make_platform_config())
            await client.start()
            try:
                res = await client.poll_gate_resolution(request_id)
            finally:
                await client.close()
            assert isinstance(res, PlatformGateResolution)
            assert res.status == expected


@pytest.mark.asyncio
async def test_poll_gate_resolution_404_returns_pending() -> None:
    """404 from the platform is a normal race (gate not yet projected)
    and must be surfaced as ``pending`` without bumping the
    gate_polls_failed counter.
    """
    request_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_bridge_get_url(request_id)).respond(
            404, json={"error": "not_found"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        try:
            res = await client.poll_gate_resolution(request_id)
        finally:
            await client.close()
        assert res.status == "pending"
        assert client.stats()["gate_polls_failed"] == 0
        assert client.stats()["gate_polls_sent"] == 1


@pytest.mark.asyncio
async def test_poll_gate_resolution_401_latches_bridge() -> None:
    """A 401 from /resolution must flip the auth-failed latch so the
    whole bridge disables (matches the audit-forward contract).
    """
    request_id = UUID("01234567-89ab-cdef-0123-456789abcdef")
    async with respx.mock(assert_all_called=False) as mock:
        mock.get(_bridge_get_url(request_id)).respond(
            401, json={"error": "invalid_token"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        try:
            res = await client.poll_gate_resolution(request_id)
            assert res.status == "pending"
            assert client.stats()["disabled"] is True
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 8. Bridge OFF default: GatesModule with no platform_client is
# pure-local (v0.7.0 parity)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gates_module_without_platform_client_is_pure_local() -> None:
    gates, _audit, audit_store = await _make_gates(platform_client=None)
    try:
        req = await gates.request("delete.user", "agent-A", payload={})
        await gates.grant(req.token)
        granted = await gates.wait_for(req.request_id, timeout=1.0)
        assert granted is True
    finally:
        await gates.close()

    # No by=platform metadata — resolution came from grant(token).
    granted_events = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "approval.granted"
    ]
    assert len(granted_events) == 1
    assert "by" not in granted_events[0].metadata


@pytest.mark.asyncio
async def test_gate_wire_format_omits_token_and_payload() -> None:
    """Regression test for the v0.7.1 wire contract: the single-use
    token and agent payload must NEVER travel to the platform.
    """
    captured_bodies: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured_bodies.append(_json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").mock(
            side_effect=_capture
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user",
                "agent-A",
                payload={"user_id": "u1", "ssn": "123-45-6789"},
            )
            for _ in range(50):
                await asyncio.sleep(0.01)
                if captured_bodies:
                    break
        finally:
            await client.close()
            await gates.close()

    assert captured_bodies, "platform POST never fired"
    body = captured_bodies[0]
    # Required fields present.
    assert body["request_id"] == str(req.request_id)
    assert body["agent_id"] == "agent-A"
    assert body["kind"] == "delete.user"
    assert body["action_hash"] == req.action_hash
    assert "expires_at" in body
    assert "sdk_version" in body
    # Forbidden fields absent.
    assert "token" not in body, "single-use token leaked to platform"
    assert "payload" not in body, "agent payload leaked to platform"
    # And the PII string from the payload dict never appears anywhere.
    import json as _json

    assert "123-45-6789" not in _json.dumps(body)


# ---------------------------------------------------------------------------
# 9. Local denial wins even when platform says granted
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_local_denied_beats_platform_granted() -> None:
    """If a human denies locally while the platform UI grants,
    the agent must see ApprovalDenied. Local always wins (invariant #1).
    """
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            200,
            json={
                "status": "granted",
                "decided_at": "2026-04-19T12:34:56+00:00",
            },
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            # Deny locally BEFORE the platform-poll tick can sync.
            await gates.deny(req.token)
            with pytest.raises(ApprovalDenied):
                await gates.wait_for(req.request_id, timeout=1.0)
        finally:
            await client.close()
            await gates.close()


# ---------------------------------------------------------------------------
# v0.7.1 pass-2 edge case A: forward-burst semaphore caps in-flight at 100
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_burst_semaphore_caps_in_flight_at_100() -> None:
    """Spawn 200 gate forwards against a hanging platform endpoint.

    At no instant should more than ``MAX_CONCURRENT_GATE_FORWARDS``
    (100) forwards be in flight. The other 100 must queue on the
    semaphore; ``gate_forwards_queued`` must record the back-pressure.
    """
    in_flight = 0
    max_observed = 0
    hang_event = asyncio.Event()

    async def _hanging_handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_observed
        in_flight += 1
        max_observed = max(max_observed, in_flight)
        try:
            await hang_event.wait()
        finally:
            in_flight -= 1
        return httpx.Response(200, json={"ok": True})

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/.*").mock(
            side_effect=_hanging_handler
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        try:
            # Fire 200 forwards in a tight burst.
            for i in range(200):
                req = ApprovalRequest(
                    request_id=UUID(int=i),
                    agent_id="agent-A",
                    kind="delete.user",
                    action_hash="b" * 64,
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(minutes=5),
                    payload={},
                    token="v1:" + "0" * 64 + f":{UUID(int=i)}:" + "b" * 64
                    + ":" + str(int(datetime.now(timezone.utc).timestamp())),
                )
                client.forward_gate_request(req)
            # Let the event loop run a bunch so tasks actually reach
            # the httpx POST (where they suspend inside the mock
            # handler).
            for _ in range(50):
                await asyncio.sleep(0.01)
                if max_observed >= 100:
                    break
        finally:
            # Release the hang so tasks can drain. close() enforces a
            # 5s flush deadline.
            hang_event.set()
            await client.close()

    # Cap was respected — at no instant were more than 100 in flight.
    assert max_observed <= 100, f"saw {max_observed} in flight, cap 100"
    # Back-pressure was observed: at least SOME forwards queued on the
    # semaphore. We don't assert ==100 because the spawn/await race
    # is timing-sensitive, but a 200-burst with a cap of 100 MUST
    # produce queued > 0.
    stats = client.stats()
    assert stats["gate_forwards_queued"] > 0, (
        f"expected back-pressure but gate_forwards_queued={stats['gate_forwards_queued']}"
    )


# ---------------------------------------------------------------------------
# v0.7.1 pass-2 edge case B: reverse-sync fires on local resolve when gate
# was forwarded; stays silent when gate was never forwarded.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reverse_sync_posts_after_local_grant_when_gate_forwarded() -> None:
    """After forward succeeds and local grant commits, notify_local_resolution
    must POST /resolution with by='sdk_local'. Platform UI now sees
    "Resolved in SDK" instead of a stale pending card.
    """
    captured: list[dict[str, Any]] = []

    def _capture_post(request: httpx.Request) -> httpx.Response:
        import json as _json

        # Separate the initial forward POST (body has request_id) from
        # the reverse-sync POST (body has decision + by).
        body = _json.loads(request.content.decode("utf-8"))
        captured.append(body)
        return httpx.Response(200, json={"ok": True})

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").mock(
            side_effect=_capture_post
        )
        mock.post(url__regex=r".*/bridge/gates/.*/resolution$").mock(
            side_effect=_capture_post
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            # Wait for forward to be ack'd.
            for _ in range(100):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_sent"] >= 1:
                    break
            # Now grant locally.
            await gates.grant(req.token)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if client.stats()["local_resolutions_sent"] >= 1:
                    break
        finally:
            await client.close()
            await gates.close()

    # Exactly one reverse-sync body.
    sync_bodies = [b for b in captured if "by" in b]
    assert len(sync_bodies) >= 1, f"no reverse-sync body in {captured!r}"
    body = sync_bodies[0]
    assert body["decision"] == "granted"
    assert body["by"] == "sdk_local"
    assert "decided_at" in body
    # Stats reflect.
    stats = client.stats()
    assert stats["local_resolutions_sent"] >= 1


@pytest.mark.asyncio
async def test_reverse_sync_silent_when_gate_never_forwarded() -> None:
    """If the platform was down at gate creation (forward failed),
    a subsequent local grant must NOT fire reverse-sync — the platform
    has no card to resolve, and a POST would 404.
    """
    async with respx.mock(assert_all_called=False) as mock:
        # All POSTs 503 — forward drops, reverse-sync must not fire
        # at all (request_id never makes it to _forwarded_request_ids).
        mock.post(url__regex=r".*/bridge/gates/.*").respond(
            503, json={"error": "service_unavailable"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            # Wait for forward drop.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if client.stats()["gate_requests_dropped"] >= 1:
                    break
            await gates.grant(req.token)
            # Give reverse-sync a chance to NOT fire.
            await asyncio.sleep(0.1)
        finally:
            await client.close()
            await gates.close()

    stats = client.stats()
    # Reverse-sync should not have run because the forward never
    # reached 2xx. local_resolutions_* stays zero.
    assert stats["local_resolutions_sent"] == 0
    assert stats["local_resolutions_dropped"] == 0


# ---------------------------------------------------------------------------
# v0.7.1 pass-2 edge case C: auth-failed latch also covers forward path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_401_latches_bridge_disables_all_subsequent() -> None:
    """A 401 on forward_gate_request must flip the auth-failed latch
    the same way poll does. After the latch, every subsequent
    forward AND every subsequent poll MUST short-circuit without I/O.
    """
    async with respx.mock(assert_all_called=False) as mock:
        # Forward 401.
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            401, json={"error": "invalid_token"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            # Wait for the 401 to land.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if client.stats()["disabled"]:
                    break
            assert client.stats()["disabled"] is True

            # A second request after the latch must SILENTLY DROP with
            # no I/O. ``gate_requests_dropped`` bumps because forward_
            # gate_request's fast-path counts it as a drop.
            drops_before = client.stats()["gate_requests_dropped"]
            req2 = await gates.request(
                "delete.user", "agent-B", payload={}
            )
            assert req2.request_id is not None
            await asyncio.sleep(0.05)
            drops_after = client.stats()["gate_requests_dropped"]
            assert drops_after > drops_before

            # Poll also short-circuits without I/O — returns pending.
            res = await client.poll_gate_resolution(req.request_id)
            assert res.status == "pending"
        finally:
            await client.close()
            await gates.close()


# ---------------------------------------------------------------------------
# v0.7.1 pass-2 edge case D: wire-format regression — strict key allowlist
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_body_has_exact_key_allowlist() -> None:
    """Regression: the forward body must contain EXACTLY the six
    allowed keys and NONE of the forbidden ones. Catches any future
    drift where a refactor accidentally leaks a token, chain key,
    or reason to the platform.
    """
    captured_bodies: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured_bodies.append(
            _json.loads(request.content.decode("utf-8"))
        )
        return httpx.Response(200, json={"ok": True})

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").mock(
            side_effect=_capture
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            await gates.request(
                "delete.user",
                "agent-A",
                payload={"ssn": "123-45-6789", "secret": "sk-live-xxx"},
            )
            for _ in range(50):
                await asyncio.sleep(0.01)
                if captured_bodies:
                    break
        finally:
            await client.close()
            await gates.close()

    assert captured_bodies, "forward never fired"
    body = captured_bodies[0]
    ALLOWED_KEYS = {
        "request_id",
        "agent_id",
        "kind",
        "action_hash",
        "expires_at",
        "sdk_version",
    }
    FORBIDDEN_KEYS = {
        "token",
        "secret",
        "payload",
        "chain_key",
        "reason",
        "decision",
        "hmac",
        "client_hmac",
    }
    body_keys = set(body.keys())
    # Exact match on allowlist.
    assert body_keys == ALLOWED_KEYS, (
        f"wire body keys drifted: got {sorted(body_keys)}, "
        f"expected {sorted(ALLOWED_KEYS)}"
    )
    # Explicit fail on every forbidden key.
    for k in FORBIDDEN_KEYS:
        assert k not in body, f"forbidden key leaked to wire: {k}"
    # And the PII string from the payload dict never appears anywhere.
    import json as _json

    assert "123-45-6789" not in _json.dumps(body)
    assert "sk-live-xxx" not in _json.dumps(body)


# ---------------------------------------------------------------------------
# v0.7.1 pass-2 edge case E: consecutive 5xx latch at 10s cap for 60s
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consecutive_5xx_latch_clamps_at_60s_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``_PLATFORM_POLL_LATCH_THRESHOLD`` (5) consecutive 5xx, the
    next poll hold must be ``_PLATFORM_POLL_LATCH_SECONDS`` (60s),
    not the normal jittered interval. Verify by observing the hold
    values scheduled after repeated failures.
    """
    import codeatelier_governance.gates.module as gates_module

    holds: list[float] = []

    # Patch the loop.time-based schedule by spying on the jittered
    # interval — when the latch fires, _jittered_interval is NOT
    # called (the module uses _PLATFORM_POLL_LATCH_SECONDS directly).
    # We capture hold values via the jittered spy to prove the latch
    # bypasses it once the failure threshold is exceeded.
    def _jitter_spy(base: float) -> float:
        holds.append(base)
        return 0.0

    monkeypatch.setattr(gates_module, "_jittered_interval", _jitter_spy)
    # Shrink the latch so tests finish quickly.
    monkeypatch.setattr(
        gates_module, "_PLATFORM_POLL_LATCH_SECONDS", 0.25
    )
    monkeypatch.setattr(
        gates_module, "_PLATFORM_POLL_LATCH_THRESHOLD", 3
    )

    async with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r".*/bridge/gates/[^/]+$").respond(
            200, json={"ok": True}
        )
        mock.get(url__regex=r".*/bridge/gates/.*/resolution$").respond(
            500, json={"error": "internal"}
        )
        client = PlatformClient(_make_platform_config())
        await client.start()
        gates, _audit, _ = await _make_gates(client)
        try:
            monkeypatch.setattr(
                gates_module, "_PLATFORM_POLL_BASE_SECONDS", 0.01
            )
            req = await gates.request(
                "delete.user", "agent-A", payload={}
            )
            with pytest.raises(ApprovalTimeout):
                await gates.wait_for(req.request_id, timeout=0.35)
        finally:
            await client.close()
            await gates.close()

    # The first few holds go through _jittered_interval (ladder:
    # 0.02 -> 0.04 -> 0.08 -> ...). Once the threshold trips, the
    # module stops calling _jittered_interval and uses the latch
    # constant instead. The spy's list must show at most 3 entries
    # (one per failure before the threshold).
    assert len(holds) <= 3, (
        f"jittered_interval called too many times; latch did not fire: {holds!r}"
    )
    # And at least the threshold number of failed polls happened.
    assert client.stats()["gate_polls_failed"] >= 3


# ---------------------------------------------------------------------------
# 10. forward_gate_request: fast-path when bridge not started
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forward_gate_request_when_not_started_counts_drop() -> None:
    """Calling forward_gate_request before start() silently drops and
    bumps gate_requests_dropped. Prevents a race where a gate
    created during SDK construction tries to POST to a client whose
    worker never spun up.
    """
    client = PlatformClient(_make_platform_config())
    # Intentionally skip start(). Construct a minimal ApprovalRequest.
    req = ApprovalRequest(
        request_id=UUID("01234567-89ab-cdef-0123-456789abcdef"),
        agent_id="agent-A",
        kind="delete.user",
        action_hash="a" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        payload={},
        token="v1:"
        + "0" * 64
        + ":01234567-89ab-cdef-0123-456789abcdef:"
        + "a" * 64
        + ":" + str(int(datetime.now(timezone.utc).timestamp())),
    )
    client.forward_gate_request(req)
    assert client.stats()["gate_requests_dropped"] == 1
    assert client.stats()["gate_requests_sent"] == 0
