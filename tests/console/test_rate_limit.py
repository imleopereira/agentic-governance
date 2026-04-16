"""Tests for F1: login rate limiting."""
from __future__ import annotations

import time

import pytest

import codeatelier_governance.console.app as _app


class TestRateLimit:
    def setup_method(self) -> None:
        _app._login_attempts.clear()

    def test_under_limit_returns_none(self) -> None:
        """5 attempts should all pass."""
        for _ in range(5):
            assert _app._check_rate_limit("1.2.3.4") is None
            _app._record_login_attempt("1.2.3.4")

    def test_over_limit_returns_retry_after(self) -> None:
        """6th attempt should be rate-limited."""
        for _ in range(5):
            _app._record_login_attempt("1.2.3.4")
        retry = _app._check_rate_limit("1.2.3.4")
        assert retry is not None
        assert retry > 0

    def test_different_ips_are_independent(self) -> None:
        """Rate limit is per-IP."""
        for _ in range(5):
            _app._record_login_attempt("1.1.1.1")
        assert _app._check_rate_limit("1.1.1.1") is not None
        assert _app._check_rate_limit("2.2.2.2") is None

    def test_stale_entries_cleaned_up(self) -> None:
        """Old attempts should be pruned."""
        # Manually insert stale timestamps (well beyond the 60s window)
        stale_time = time.monotonic() - 120
        _app._login_attempts["1.2.3.4"] = [stale_time, stale_time, stale_time, stale_time, stale_time]
        result = _app._check_rate_limit("1.2.3.4")
        # After pruning, the list should be empty so no rate limit
        assert result is None, f"Expected None but got {result}, attempts={_app._login_attempts.get('1.2.3.4')}"

    def test_cleanup_removes_stale_ips(self) -> None:
        """Opportunistic cleanup should remove IPs with only stale entries."""
        _app._login_attempts["stale.ip"] = [time.monotonic() - 120]
        _app._record_login_attempt("fresh.ip")
        assert "stale.ip" not in _app._login_attempts


# ---------------------------------------------------------------------------
# F6 Track B: per-user rate limit on authenticated endpoints
# ---------------------------------------------------------------------------
class TestPerUserRateLimit:
    def setup_method(self) -> None:
        _app._user_request_times.clear()

    def test_under_limit_returns_none(self) -> None:
        for _ in range(_app._USER_RATE_LIMIT_MAX):
            assert _app._check_user_rate_limit("user-a") is None

    def test_over_limit_returns_retry_after(self) -> None:
        """The 61st request should be rate-limited."""
        for _ in range(_app._USER_RATE_LIMIT_MAX):
            _app._check_user_rate_limit("user-a")
        retry = _app._check_user_rate_limit("user-a")
        assert retry is not None
        assert retry > 0

    def test_different_users_independent(self) -> None:
        for _ in range(_app._USER_RATE_LIMIT_MAX):
            _app._check_user_rate_limit("user-a")
        assert _app._check_user_rate_limit("user-a") is not None
        assert _app._check_user_rate_limit("user-b") is None

    def test_stale_entries_pruned(self) -> None:
        _app._user_request_times["user-c"] = [
            time.monotonic() - 120
        ] * _app._USER_RATE_LIMIT_MAX
        # Stale entries mean the user is no longer rate-limited.
        assert _app._check_user_rate_limit("user-c") is None

    def test_default_max_is_60(self) -> None:
        """Default cap for the per-user rate limit is 60 req/min."""
        assert _app._USER_RATE_LIMIT_MAX == 60

    def test_rate_limit_dependency_raises_429(self) -> None:
        """``rate_limit_per_user`` raises HTTPException(429) when over quota."""
        import asyncio

        from fastapi import HTTPException

        class _FakeState:
            user_id = "quota-user"

        class _FakeReq:
            state = _FakeState()

        async def _run() -> None:
            for _ in range(_app._USER_RATE_LIMIT_MAX):
                _app._check_user_rate_limit("quota-user")
            with pytest.raises(HTTPException) as exc_info:
                await _app.rate_limit_per_user(_FakeReq())  # type: ignore[arg-type]
            assert exc_info.value.status_code == 429

        asyncio.run(_run())
