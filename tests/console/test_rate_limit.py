"""Tests for F1: login rate limiting."""
from __future__ import annotations

import time

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
