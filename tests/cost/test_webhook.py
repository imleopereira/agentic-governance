"""Tests for the budget-alert webhook (F2, v0.7.0).

Covers:
    * first-crossing fires exactly one POST (signed)
    * second-crossing in same day fires zero POSTs
    * HMAC signature round-trips via ``verify_signature``
    * Canonical JSON is stable across key ordering
    * SSRF allowlist rejects RFC1918 literal hosts
    * SSRF allowlist rejects AWS/GCP metadata host
    * SSRF allowlist rejects loopback + non-http schemes
    * Flag-off via env suppresses all POSTs
    * Non-2xx response triggers exactly one retry then drop
    * Missing secret refuses to send
    * Negative crossing (usage already above threshold) does not fire
"""
from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from codeatelier_governance.cost import BudgetPolicy, CostModule
from codeatelier_governance.cost.webhook import (
    ENV_WEBHOOKS_ENABLED,
    WebhookConfigError,
    _assert_url_safe,
    build_payload,
    period_bucket_utc_day,
    send_budget_alert,
    sign_payload,
    verify_signature,
    _canonical_json,
)


# ---------------------------------------------------------------------------
# Fake httpx client wired via module injection
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in."""

    posts: list[dict[str, Any]] = []
    responses: list[int] = [200]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(
        self, url: str, content: bytes, headers: dict[str, str]
    ) -> _FakeResponse:
        type(self).posts.append(
            {"url": url, "content": content, "headers": dict(headers)}
        )
        idx = min(len(type(self).posts) - 1, len(type(self).responses) - 1)
        status = type(self).responses[idx]
        return _FakeResponse(status)


@pytest.fixture(autouse=True)
def _reset_fake_client() -> None:
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.responses = [200]


@pytest.fixture
def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Inject a fake httpx module only for the duration of this test."""
    import sys
    import types

    fake = types.ModuleType("httpx")
    fake.AsyncClient = _FakeAsyncClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "httpx", fake)
    return fake


@pytest.fixture
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_WEBHOOKS_ENABLED, "true")


@pytest.fixture
def _flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_WEBHOOKS_ENABLED, "false")


# ---------------------------------------------------------------------------
# SSRF allowlist
# ---------------------------------------------------------------------------
def test_ssrf_reject_rfc1918_10() -> None:
    with pytest.raises(WebhookConfigError, match="private"):
        _assert_url_safe("http://10.0.0.1/webhook")


def test_ssrf_reject_rfc1918_172() -> None:
    with pytest.raises(WebhookConfigError, match="private"):
        _assert_url_safe("http://172.16.5.5/hook")


def test_ssrf_reject_rfc1918_192() -> None:
    with pytest.raises(WebhookConfigError, match="private"):
        _assert_url_safe("http://192.168.1.1/hook")


def test_ssrf_reject_aws_metadata() -> None:
    # 169.254.169.254 -> link-local AND in explicit metadata blocklist
    with pytest.raises(WebhookConfigError):
        _assert_url_safe("http://169.254.169.254/latest/meta-data/")


def test_ssrf_reject_gcp_metadata() -> None:
    with pytest.raises(WebhookConfigError, match="metadata"):
        _assert_url_safe("http://metadata.google.internal/")


def test_ssrf_reject_loopback() -> None:
    with pytest.raises(WebhookConfigError):
        _assert_url_safe("http://127.0.0.1:9000/")


def test_ssrf_reject_non_http_scheme() -> None:
    with pytest.raises(WebhookConfigError, match="scheme"):
        _assert_url_safe("file:///etc/passwd")


def test_ssrf_reject_no_host() -> None:
    with pytest.raises(WebhookConfigError, match="host"):
        _assert_url_safe("https:///just-a-path")


def test_ssrf_accepts_public_https() -> None:
    # 8.8.8.8 is public — only allowlist-rejects private/reserved.
    _assert_url_safe("https://hooks.slack.com/services/abc/def/xyz")
    _assert_url_safe("https://8.8.8.8/ingest")


# ---------------------------------------------------------------------------
# Canonical JSON + HMAC
# ---------------------------------------------------------------------------
def test_canonical_json_is_sort_key_stable() -> None:
    a = {"b": 2, "a": 1, "c": 3}
    b = {"c": 3, "a": 1, "b": 2}
    assert _canonical_json(a) == _canonical_json(b)


def test_canonical_json_strips_signature() -> None:
    base = {"a": 1, "b": 2}
    signed = {"a": 1, "b": 2, "signature": "sha256=deadbeef"}
    assert _canonical_json(base) == _canonical_json(signed)


def test_sign_and_verify_roundtrip() -> None:
    payload = build_payload(
        agent_id="billing-agent",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.5,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="fixed-nonce-for-test",
    )
    sig = sign_payload(payload, secret="s3cret")
    assert sig.startswith("sha256=")
    assert verify_signature(payload, "s3cret", sig) is True


def test_verify_rejects_wrong_secret() -> None:
    payload = build_payload(
        agent_id="a",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.5,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="n",
    )
    sig = sign_payload(payload, secret="right")
    assert verify_signature(payload, "wrong", sig) is False


def test_verify_rejects_tampered_payload() -> None:
    p = build_payload(
        agent_id="a",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.5,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="n",
    )
    sig = sign_payload(p, secret="s")
    p["used_value"] = 0.01
    assert verify_signature(p, "s", sig) is False


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_send_posts_once_on_2xx(_install_fake_httpx: Any) -> None:
    _FakeAsyncClient.responses = [200]
    payload = build_payload(
        agent_id="a",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.1,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="n",
    )
    ok = await send_budget_alert(
        url="https://hooks.example.com/x",
        secret="s",
        payload=payload,
    )
    assert ok is True
    assert len(_FakeAsyncClient.posts) == 1
    headers = _FakeAsyncClient.posts[0]["headers"]
    assert headers["X-Governance-Signature"].startswith("sha256=")


@pytest.mark.asyncio
async def test_send_retries_once_on_500(_install_fake_httpx: Any) -> None:
    _FakeAsyncClient.responses = [500, 200]
    payload = build_payload(
        agent_id="a",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.1,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="n",
    )
    ok = await send_budget_alert(
        url="https://hooks.example.com/x", secret="s", payload=payload
    )
    assert ok is True
    assert len(_FakeAsyncClient.posts) == 2


@pytest.mark.asyncio
async def test_send_drops_after_two_non2xx(_install_fake_httpx: Any) -> None:
    _FakeAsyncClient.responses = [500, 500]
    payload = build_payload(
        agent_id="a",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.1,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="n",
    )
    ok = await send_budget_alert(
        url="https://hooks.example.com/x", secret="s", payload=payload
    )
    assert ok is False
    assert len(_FakeAsyncClient.posts) == 2


@pytest.mark.asyncio
async def test_send_rejects_ssrf_before_network(_install_fake_httpx: Any) -> None:
    payload = build_payload(
        agent_id="a",
        cap_id="per_agent_usd_daily",
        cap_type="per_agent_usd_daily",
        cap_value=10.0,
        used_value=8.1,
        threshold_pct=80,
        period_bucket="2026-04-18",
        nonce="n",
    )
    with pytest.raises(WebhookConfigError):
        await send_budget_alert(
            url="http://169.254.169.254/latest/meta-data/",
            secret="s",
            payload=payload,
        )
    assert _FakeAsyncClient.posts == []


# ---------------------------------------------------------------------------
# End-to-end via CostModule
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_crossing_fires_once(
    cost: CostModule, _install_fake_httpx: Any, _flag_on: None
) -> None:
    cost.register(
        BudgetPolicy(
            agent_id="billing",
            per_agent_usd_daily=10.0,
            alert_webhook_url="https://hooks.example.com/b",
            alert_webhook_secret="s3cret",  # type: ignore[arg-type]
            alert_threshold_pct=80,
        )
    )
    sid = uuid4()
    # Go below threshold
    await cost.track("billing", sid, usd=4.0)
    assert _FakeAsyncClient.posts == []
    # Cross threshold (4 -> 9, crosses 8.0)
    await cost.track("billing", sid, usd=5.0)
    assert len(_FakeAsyncClient.posts) == 1
    body = _FakeAsyncClient.posts[0]["content"]
    assert b'"agent_id"' in body and b'"billing"' in body
    assert b'"threshold_pct"' in body and b'80' in body


@pytest.mark.asyncio
async def test_duplicate_crossing_same_day_does_not_fire(
    cost: CostModule, _install_fake_httpx: Any, _flag_on: None
) -> None:
    cost.register(
        BudgetPolicy(
            agent_id="billing",
            per_agent_usd_daily=10.0,
            alert_webhook_url="https://hooks.example.com/b",
            alert_webhook_secret="s",  # type: ignore[arg-type]
            alert_threshold_pct=80,
        )
    )
    sid = uuid4()
    await cost.track("billing", sid, usd=8.1)  # cross
    await cost.track("billing", sid, usd=0.1)  # still above
    await cost.track("billing", sid, usd=0.5)  # further above
    assert len(_FakeAsyncClient.posts) == 1


@pytest.mark.asyncio
async def test_flag_off_suppresses_post(
    cost: CostModule, _install_fake_httpx: Any, _flag_off: None
) -> None:
    cost.register(
        BudgetPolicy(
            agent_id="billing",
            per_agent_usd_daily=10.0,
            alert_webhook_url="https://hooks.example.com/b",
            alert_webhook_secret="s",  # type: ignore[arg-type]
            alert_threshold_pct=80,
        )
    )
    sid = uuid4()
    await cost.track("billing", sid, usd=9.5)  # would cross
    assert _FakeAsyncClient.posts == []


@pytest.mark.asyncio
async def test_no_webhook_configured_is_noop(
    cost: CostModule, _install_fake_httpx: Any, _flag_on: None
) -> None:
    cost.register(
        BudgetPolicy(agent_id="billing", per_agent_usd_daily=10.0)
    )
    sid = uuid4()
    await cost.track("billing", sid, usd=9.9)
    assert _FakeAsyncClient.posts == []


@pytest.mark.asyncio
async def test_missing_secret_refuses_post(
    cost: CostModule, _install_fake_httpx: Any, _flag_on: None
) -> None:
    cost.register(
        BudgetPolicy(
            agent_id="billing",
            per_agent_usd_daily=10.0,
            alert_webhook_url="https://hooks.example.com/b",
            alert_threshold_pct=80,
            # No secret — must not fire unsigned.
        )
    )
    sid = uuid4()
    await cost.track("billing", sid, usd=9.5)
    assert _FakeAsyncClient.posts == []


@pytest.mark.asyncio
async def test_policy_construction_rejects_webhook_without_daily_cap() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="per_agent_usd_daily"):
        BudgetPolicy(
            agent_id="billing",
            per_session_usd=5.0,  # session-only, no daily cap
            alert_webhook_url="https://hooks.example.com/b",
            alert_webhook_secret="s",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Period bucket
# ---------------------------------------------------------------------------
def test_period_bucket_utc_day_is_ymd() -> None:
    from datetime import datetime, timezone

    s = period_bucket_utc_day(datetime(2026, 4, 18, 23, 59, tzinfo=timezone.utc))
    assert s == "2026-04-18"


# Silence unused-fixture linting in clearly-standalone tests above.
_ = asyncio
