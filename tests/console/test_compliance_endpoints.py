"""F4: /api/compliance/report and /api/compliance/verify-chain.

Covers:
  * Pydantic response models are strict (extra forbid, literal enums).
  * The 1 req/60 s/user rate limiter fires on the second call and
    returns a usable ``retry_after``.
  * The module-level short-circuit cache returns the same payload
    within the TTL and a fresh payload once the cache entry is evicted.
  * The global semaphore bounds concurrent compliance work at 2.
  * The chain-status vocabulary mapping (``failed`` -> ``halted``) is
    applied at the API boundary.
  * The pill timestamp is serialized at minute granularity.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import codeatelier_governance.console.app as _app
from codeatelier_governance.console.models.responses import (
    ComplianceReportView,
    VerifyChainResponse,
)


# ---------- Model-level tests --------------------------------------------


def test_compliance_report_view_is_strict_forbid() -> None:
    assert ComplianceReportView.model_config.get("extra") == "forbid"
    assert ComplianceReportView.model_config.get("strict") is True


def test_verify_chain_response_is_strict_forbid() -> None:
    assert VerifyChainResponse.model_config.get("extra") == "forbid"
    assert VerifyChainResponse.model_config.get("strict") is True


def test_compliance_report_view_rejects_unknown_status() -> None:
    from uuid import uuid4

    with pytest.raises(ValidationError):
        ComplianceReportView(
            report_id=uuid4(),
            generated_at=datetime.now(timezone.utc),
            chain_integrity_status="mystery",  # type: ignore[arg-type]
            chain_verified_from_seq=None,
            chain_verified_to_seq=None,
            coverage_pct=None,
            coverage_pct_reason=None,
            coverage_caveat=None,
            total_events_audited=0,
            total_agents=0,
        )


def test_compliance_report_view_accepts_all_four_statuses() -> None:
    from uuid import uuid4

    for status in ("verified", "unverified", "degraded", "halted"):
        v = ComplianceReportView(
            report_id=uuid4(),
            generated_at=datetime.now(timezone.utc),
            chain_integrity_status=status,  # type: ignore[arg-type]
            chain_verified_from_seq=None,
            chain_verified_to_seq=None,
            coverage_pct=None,
            coverage_pct_reason=None,
            coverage_caveat=None,
            total_events_audited=0,
            total_agents=0,
        )
        assert v.chain_integrity_status == status


def test_verify_chain_response_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        VerifyChainResponse(
            chain_integrity_status="verified",
            from_seq=0,
            to_seq=10,
            verified_count=11,
            failed_count=0,
            unresolved_fingerprints=[],
            verified_at_utc=datetime.now(timezone.utc),
            sneaky="leak",  # type: ignore[call-arg]
        )


# ---------- Helper-level tests -------------------------------------------


class TestComplianceRateLimit:
    def setup_method(self) -> None:
        _app._compliance_user_times.clear()

    def test_first_call_passes(self) -> None:
        assert _app._compliance_rate_limit_check("alice") is None

    def test_second_call_within_window_is_blocked(self) -> None:
        assert _app._compliance_rate_limit_check("alice") is None
        retry = _app._compliance_rate_limit_check("alice")
        assert retry is not None
        assert retry > 0

    def test_rate_limit_is_per_user(self) -> None:
        assert _app._compliance_rate_limit_check("alice") is None
        # Bob's quota is not affected by Alice's.
        assert _app._compliance_rate_limit_check("bob") is None


class TestComplianceCache:
    def setup_method(self) -> None:
        _app._compliance_cache.clear()

    def test_get_returns_none_when_empty(self) -> None:
        assert _app._compliance_cache_get("k") is None

    def test_put_then_get_returns_payload(self) -> None:
        _app._compliance_cache_put("k", {"value": 1})
        assert _app._compliance_cache_get("k") == {"value": 1}

    def test_expired_entry_is_evicted(self) -> None:
        import time

        _app._compliance_cache_put("k", {"value": 1})
        # Rewind the inserted_at so the entry is past TTL.
        inserted_at, payload = _app._compliance_cache["k"]
        _app._compliance_cache["k"] = (
            inserted_at - (_app._COMPLIANCE_CACHE_TTL_SECONDS + 5),
            payload,
        )
        assert _app._compliance_cache_get("k") is None


class TestComplianceSemaphore:
    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrency_at_two(self) -> None:
        """At most 2 callers hold the compliance semaphore at once."""
        peak = [0]
        current = [0]
        lock = asyncio.Lock()

        async def worker() -> None:
            async with _app._COMPLIANCE_SEMAPHORE:
                async with lock:
                    current[0] += 1
                    if current[0] > peak[0]:
                        peak[0] = current[0]
                await asyncio.sleep(0.02)
                async with lock:
                    current[0] -= 1

        await asyncio.gather(*[worker() for _ in range(10)])
        assert peak[0] <= 2


# ---------- Status mapping ------------------------------------------------


def test_map_chain_status_failed_to_halted() -> None:
    assert _app._map_chain_status("failed") == "halted"


def test_map_chain_status_passes_through_known_values() -> None:
    for v in ("verified", "unverified", "degraded", "halted"):
        assert _app._map_chain_status(v) == v


def test_map_chain_status_unknown_defaults_to_unverified() -> None:
    assert _app._map_chain_status("whatever") == "unverified"


# ---------- Minute-granularity timestamps --------------------------------


def test_round_to_minute_strips_seconds_and_micros() -> None:
    ts = datetime(2026, 4, 15, 14, 22, 37, 501_000, tzinfo=timezone.utc)
    rounded = _app._round_to_minute(ts)
    assert rounded == datetime(2026, 4, 15, 14, 22, 0, 0, tzinfo=timezone.utc)
    assert rounded.second == 0
    assert rounded.microsecond == 0


# ---------- Endpoint registration ----------------------------------------


def test_compliance_endpoints_registered() -> None:
    routes = {r.path for r in _app.app.routes}  # type: ignore[attr-defined]
    assert "/api/compliance/report" in routes
    assert "/api/compliance/verify-chain" in routes


# ---------- DA Wave 4 edge cases -----------------------------------------


@pytest.mark.asyncio
async def test_compliance_report_verify_called_exactly_once() -> None:
    """BLOCKER 1: /api/compliance/report must not re-run verify_chain."""
    from uuid import uuid4

    from codeatelier_governance.compliance.report import ReportGenerator

    _app._compliance_cache.clear()

    class _CountingAudit:
        def __init__(self) -> None:
            self.calls = 0

        async def verify_chain(
            self, *, from_seq: int | None = None, to_seq: int | None = None
        ) -> None:
            self.calls += 1

    class _MemStore:
        _events: dict[Any, Any] = {}

        async def get_session_events(self, _sid: Any) -> list[Any]:
            return []

    audit = _CountingAudit()
    gen = ReportGenerator(
        audit_store=_MemStore(),  # type: ignore[arg-type]
        audit_module=audit,
    )
    report = await gen.generate_article12(verify_chain=True)
    assert audit.calls == 1
    # The handler now reads these directly off the report — no second call.
    assert hasattr(report, "chain_verified_from_seq")
    assert hasattr(report, "chain_verified_to_seq")
    _ = (report.chain_verified_from_seq, report.chain_verified_to_seq)
    _ = uuid4  # silence unused import if pruned


def test_compliance_rate_limit_unauthenticated_uses_anon_bucket() -> None:
    """BLOCKER C3: unauthenticated callers now share a single global
    anonymous bucket (1 call per 300 s). The first call passes, the
    second must 429. This replaces the v0.5.x behavior where anonymous
    callers bypassed the limit entirely."""
    import asyncio

    from fastapi import HTTPException, Request

    _app._compliance_user_times.clear()
    _app._compliance_anon_times.clear()

    async def _run() -> None:
        scope: dict[str, Any] = {
            "type": "http",
            "headers": [],
            "method": "GET",
            "path": "/",
        }
        req = Request(scope)  # type: ignore[arg-type]
        # First anonymous call passes.
        await _app._compliance_rate_limit_dep(req)
        # Second anonymous call within 300 s must 429.
        with pytest.raises(HTTPException) as excinfo:
            await _app._compliance_rate_limit_dep(req)
        assert excinfo.value.status_code == 429

    asyncio.get_event_loop().run_until_complete(_run())
    _app._compliance_anon_times.clear()


def test_compliance_rate_limit_window_boundary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """At the exact window boundary the next call MUST be allowed."""
    import time as _time

    _app._compliance_user_times.clear()
    t = [1000.0]
    monkeypatch.setattr(_time, "monotonic", lambda: t[0])
    # Rewire the monotonic lookup inside the module too.
    monkeypatch.setattr(_app.time, "monotonic", lambda: t[0])

    assert _app._compliance_rate_limit_check("u") is None  # first ok
    assert _app._compliance_rate_limit_check("u") is not None  # blocked
    # Advance past the window.
    t[0] += _app._COMPLIANCE_RATE_LIMIT_WINDOW_SECONDS + 1
    assert _app._compliance_rate_limit_check("u") is None  # allowed again


@pytest.mark.asyncio
async def test_compliance_semaphore_max_two_concurrent() -> None:
    """Third concurrent caller blocks until one of the first two releases."""
    # Rebind the module-level semaphore on THIS loop to avoid the
    # cross-loop binding that asyncio.Semaphore picks up at first use.
    old = _app._COMPLIANCE_SEMAPHORE
    _app._COMPLIANCE_SEMAPHORE = asyncio.Semaphore(2)
    try:
        started: list[int] = []
        release_first = asyncio.Event()
        release_all = asyncio.Event()

        async def worker(idx: int) -> None:
            async with _app._COMPLIANCE_SEMAPHORE:
                started.append(idx)
                if idx < 2:
                    await release_first.wait()
                else:
                    await release_all.wait()

        t1 = asyncio.create_task(worker(0))
        t2 = asyncio.create_task(worker(1))
        t3 = asyncio.create_task(worker(2))
        await asyncio.sleep(0.05)
        assert len(started) == 2  # third is blocked
        release_first.set()
        await asyncio.sleep(0.05)
        assert len(started) == 3  # now third acquired
        release_all.set()
        await asyncio.gather(t1, t2, t3)
    finally:
        _app._COMPLIANCE_SEMAPHORE = old


def test_compliance_cache_stale_eviction() -> None:
    """A stale cache entry MUST not be returned and MUST be evicted."""
    _app._compliance_cache.clear()
    _app._compliance_cache_put("k", {"v": 1})
    inserted_at, payload = _app._compliance_cache["k"]
    _app._compliance_cache["k"] = (
        inserted_at - (_app._COMPLIANCE_CACHE_TTL_SECONDS + 5),
        payload,
    )
    assert _app._compliance_cache_get("k") is None
    assert "k" not in _app._compliance_cache


def test_map_chain_status_unknown_returns_unverified() -> None:
    assert _app._map_chain_status("totally_bogus_value") == "unverified"


def test_verify_chain_response_rotation_aware_false_default() -> None:
    """SHIP-WITH-CHANGES 3: the v0.6 verify-chain response defaults rotation_aware=False."""
    r = VerifyChainResponse(
        chain_integrity_status="verified",
        from_seq=0,
        to_seq=10,
        verified_count=11,
        failed_count=0,
        unresolved_fingerprints=[],
        verified_at_utc=datetime.now(timezone.utc),
    )
    assert r.rotation_aware is False
    # Empty fingerprint list is NOT a verification signal on this path.
    assert r.unresolved_fingerprints == []


def test_compliance_report_rotation_aware_field_present() -> None:
    from uuid import uuid4

    v = ComplianceReportView(
        report_id=uuid4(),
        generated_at=datetime.now(timezone.utc),
        chain_integrity_status="verified",
        chain_verified_from_seq=0,
        chain_verified_to_seq=10,
        coverage_pct=None,
        coverage_pct_reason=None,
        coverage_caveat=None,
        total_events_audited=0,
        total_agents=0,
    )
    assert v.rotation_aware is False
