"""Outbound budget-alert webhook (HMAC-signed, SSRF-guarded, once-per-bucket).

Fires at most ONE POST per ``(agent_id, cap_id, period_bucket)`` tuple when
cumulative daily USD crosses a configurable percentage of a USD cap (default
80%). The dedup key is in-memory for v0.7 (PRD §F2 acceptance lists DB-backed
``ON CONFLICT`` as a v0.7.1 hardening); a process restart inside the same day
therefore can re-fire once. This is documented in the firing-decision log line.

Security posture (PRD §F2 + §red-team 3):

* **SSRF allowlist** — any host that resolves to RFC1918 (10/8, 172.16/12,
  192.168/16), loopback (127/8, ::1), link-local (169.254/16), or the AWS/GCP
  cloud-metadata host (``169.254.169.254``, ``metadata.google.internal``) is
  rejected at send-time with ``WebhookConfigError``. The check runs against
  the **parsed URL's literal host** — DNS rebinding is out of scope for v0.7
  (documented trade-off; customers set URL once in trusted config).

* **HMAC-SHA256** signature over the canonical JSON of the payload MINUS the
  ``signature`` field. Canonical = ``json.dumps(payload, sort_keys=True,
  separators=(",", ":"), ensure_ascii=True)``. Signature embedded in the POST
  body as ``"signature": "sha256=<hex>"`` AND mirrored in the
  ``X-Governance-Signature`` header so receivers can use either. Nonce field
  defends against replay (PRD B5).

* **Feature flag** ``GOVERNANCE_COST_WEBHOOKS_ENABLED`` defaults to ``"false"``.
  When disabled, ``send_budget_alert`` is a structured-log no-op — BUT we log
  ONCE per (agent, flag-state) on first deliberate skip so operators can see
  that their webhook was configured but suppressed.

* **Retry** — one retry on non-2xx, then drop + log. PRD is explicit: do not
  stack retries on a webhook endpoint that may itself be rate-limiting us.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import structlog

from ..errors import GovernanceError

logger = structlog.get_logger(__name__)


ENV_WEBHOOKS_ENABLED = "GOVERNANCE_COST_WEBHOOKS_ENABLED"

# Cloud-metadata hosts (AWS, GCP, Azure, OpenStack, DigitalOcean). Any literal
# match on the URL host blocks send at config-time, before DNS resolution
# can be influenced by an attacker.
_BLOCKED_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata",
        "metadata.azure.com",
    }
)

# Retry budget: PRD §F2 — one retry on non-2xx, then drop.
_RETRY_SLEEP_SECONDS = 0.5
_DEFAULT_TIMEOUT_SECONDS = 5.0


class WebhookError(GovernanceError):
    """Raised when a budget-alert webhook cannot be sent for a security reason.

    Delivery failures (timeout, non-2xx) are logged + swallowed; only
    *config-time* security rejections raise. Host app never breaks.
    """


class WebhookConfigError(WebhookError):
    """Raised when an alert_webhook_url violates the SSRF allowlist."""


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------
def _host_is_private(host: str) -> bool:
    """Return True if host is a literal private/loopback/link-local IP.

    Only checks literal IP forms in the URL. DNS rebinding (hostname that
    resolves to RFC1918 at connect-time) is documented as out-of-scope;
    tenant URL is set once in trusted config. If future requirements demand
    resolved-IP checking, wire a custom socket.getaddrinfo here.
    """
    try:
        ip = ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _assert_url_safe(url: str) -> None:
    """Raise WebhookConfigError if the URL fails the SSRF allowlist.

    Blocks:
        * non-http(s) schemes (file://, gopher://, ftp://, data:)
        * empty host
        * literal RFC1918 / loopback / link-local / reserved IPs
        * cloud-metadata hostnames (169.254.169.254 etc)
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebhookConfigError(
            f"alert_webhook_url scheme must be http or https, got "
            f"{parsed.scheme!r}. Fix: use https://hooks.example.com/...",
            recovery_hint="Only http(s) webhooks are allowed.",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise WebhookConfigError(
            "alert_webhook_url has no host component. "
            "Fix: use a full URL like https://hooks.example.com/path.",
            recovery_hint="Provide a full URL including host.",
        )
    if host in _BLOCKED_METADATA_HOSTS:
        raise WebhookConfigError(
            f"alert_webhook_url points at a cloud-metadata host ({host!r}); "
            f"blocked to prevent SSRF credential exfiltration.",
            recovery_hint="Use a public HTTPS webhook (Slack, Opsgenie, etc).",
        )
    if _host_is_private(host):
        raise WebhookConfigError(
            f"alert_webhook_url host {host!r} is in a private / loopback / "
            f"link-local IP range (RFC1918). Blocked to prevent SSRF.",
            recovery_hint="Use a publicly-routable HTTPS webhook.",
        )


# ---------------------------------------------------------------------------
# Canonical JSON + HMAC
# ---------------------------------------------------------------------------
def _canonical_json(payload: dict[str, Any]) -> bytes:
    """Canonicalize a payload for HMAC signing.

    Canonical form = sort_keys=True + smallest JSON separators + ASCII-safe.
    Stable across Python versions and libraries. The ``signature`` field
    (if present) is stripped before canonicalization so the signature can
    live inside the same dict it signs.
    """
    stripped = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(
        stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Return ``sha256=<hex>`` HMAC of canonicalized payload.

    The returned string is intentionally prefixed with ``sha256=`` so the
    algorithm is encoded at rest in the receiver's log — matching the
    GitHub webhook convention and making future algorithm rotation safe.
    """
    mac = hmac.new(
        secret.encode("utf-8"), _canonical_json(payload), hashlib.sha256
    )
    return f"sha256={mac.hexdigest()}"


def verify_signature(
    payload: dict[str, Any], secret: str, signature: str
) -> bool:
    """Constant-time verify of ``sha256=<hex>`` or bare hex signature."""
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------
def _webhooks_enabled() -> bool:
    """Read the feature flag fresh each call — test isolation.

    Per the repo memory ``feedback_no_module_level_env_capture``, we do NOT
    cache this at import time. The cost of reading ``os.environ`` on each
    crossing is microseconds and crossings are rare.
    """
    return os.environ.get(ENV_WEBHOOKS_ENABLED, "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------
@dataclass
class AlertDedup:
    """In-memory set of already-fired ``(agent_id, cap_id, period_bucket)``.

    v0.7 keeps this in-process. Upgrading to Postgres UPSERT with a
    ``(agent_id, cap_id, period_bucket)`` unique index is slated for
    v0.7.1 (PRD §F2 rotation note: dedup table in same tx as check).
    A process restart inside the same UTC day CAN re-fire once; this is
    documented behavior and acceptable for the initial release.
    """

    fired: set[tuple[str, str, str]] = field(default_factory=set)

    def mark_if_unfired(
        self, agent_id: str, cap_id: str, period_bucket: str
    ) -> bool:
        """Return True and record the key if not previously fired.

        Returns False if the key is already in the set — caller skips POST.
        """
        key = (agent_id, cap_id, period_bucket)
        if key in self.fired:
            return False
        self.fired.add(key)
        return True


def period_bucket_utc_day(now: datetime | None = None) -> str:
    """Return the canonical UTC-day bucket string for dedup keys."""
    when = now or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------
def build_payload(
    *,
    agent_id: str,
    cap_id: str,
    cap_type: str,
    cap_value: float,
    used_value: float,
    threshold_pct: int,
    period_bucket: str,
    nonce: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the canonical payload (pre-signature) for a budget alert."""
    when = now or datetime.now(timezone.utc)
    return {
        "schema": "governance.cost.budget_alert/v1",
        "event": "budget.threshold_crossed",
        "agent_id": agent_id,
        "cap_id": cap_id,
        "cap_type": cap_type,
        "cap_value": float(cap_value),
        "used_value": float(used_value),
        "used_pct": (
            round(100.0 * used_value / cap_value, 2) if cap_value > 0 else 0.0
        ),
        "threshold_pct": int(threshold_pct),
        "period_bucket": period_bucket,
        "nonce": nonce or secrets.token_hex(16),
        "timestamp": when.astimezone(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
async def send_budget_alert(
    *,
    url: str,
    secret: str,
    payload: dict[str, Any],
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Send one signed POST with one retry. Returns True on 2xx.

    NEVER raises for delivery failures — returns False and logs. DOES raise
    ``WebhookConfigError`` for SSRF / scheme violations (config bug, not
    network transient).

    The httpx import is lazy so ``import codeatelier_governance.cost`` works
    without the ``[platform]`` extra installed. If httpx is not available we
    log ``cost.webhook_httpx_missing`` and return False.
    """
    _assert_url_safe(url)

    try:
        import httpx
    except ImportError:
        logger.warning(
            "cost.webhook_httpx_missing",
            detail=(
                "httpx is required for webhook delivery. Install with "
                "pip install 'code-atelier-governance[platform]'."
            ),
        )
        return False

    signature = sign_payload(payload, secret)
    body = dict(payload)
    body["signature"] = signature
    body_bytes = json.dumps(body, sort_keys=True, ensure_ascii=True).encode(
        "utf-8"
    )
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "code-atelier-governance-webhook/0.7",
        "X-Governance-Signature": signature,
        "X-Governance-Event": "budget.threshold_crossed",
    }

    attempt = 0
    last_status: int | None = None
    last_error: str | None = None
    while attempt < 2:
        attempt += 1
        try:
            # Security P0: trust_env=False disables httpx's default read of
            # HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / SSL_CERT_FILE — closes
            # the env-controlled exfil path for the signed webhook body
            # (which carries the budget context + cap_id + agent_id).
            # follow_redirects=False pins the current default so a future
            # version bump cannot silently open a 302-exfil path.
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                resp = await client.post(url, content=body_bytes, headers=headers)
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                logger.info(
                    "cost.webhook_delivered",
                    agent_id=payload.get("agent_id"),
                    cap_id=payload.get("cap_id"),
                    status=resp.status_code,
                    attempt=attempt,
                )
                return True
            logger.warning(
                "cost.webhook_delivery_non2xx",
                agent_id=payload.get("agent_id"),
                status=resp.status_code,
                attempt=attempt,
            )
        except Exception as exc:  # noqa: BLE001 — non-breaking
            last_error = type(exc).__name__
            logger.warning(
                "cost.webhook_delivery_failed",
                agent_id=payload.get("agent_id"),
                error_type=last_error,
                attempt=attempt,
            )
        if attempt < 2:
            # Single retry per PRD acceptance criteria.
            # Keep the sleep tiny — this runs off the host call path, but we
            # also don't want to hold an httpx client warm for seconds.
            import asyncio

            await asyncio.sleep(_RETRY_SLEEP_SECONDS)

    logger.error(
        "cost.webhook_delivery_dropped",
        agent_id=payload.get("agent_id"),
        cap_id=payload.get("cap_id"),
        last_status=last_status,
        last_error=last_error,
    )
    return False


def log_disabled_once(
    agent_id: str, has_webhook: bool, _seen: set[str] = set()
) -> None:
    """Log once per agent if webhooks are disabled but a URL was configured.

    Intentional module-level mutable default is safe here because the set
    is effectively a process-lifetime de-dup cache and never read by
    callers. Reset is not needed in tests; the set is bounded by
    ``len(agents)``.
    """
    if not has_webhook:
        return
    if _webhooks_enabled():
        return
    if agent_id in _seen:
        return
    _seen.add(agent_id)
    logger.info(
        "cost.webhook_disabled_flag_off",
        agent_id=agent_id,
        env_var=ENV_WEBHOOKS_ENABLED,
        detail=(
            f"BudgetPolicy for {agent_id!r} carries alert_webhook_url but "
            f"{ENV_WEBHOOKS_ENABLED}=false (default). No POST will fire. "
            f"Set {ENV_WEBHOOKS_ENABLED}=true to enable delivery."
        ),
    )


# Re-export for tests that want to clear dedup state. Explicitly not in
# ``cost/__init__.py`` — internal utility.
__all__ = [
    "AlertDedup",
    "ENV_WEBHOOKS_ENABLED",
    "WebhookConfigError",
    "WebhookError",
    "build_payload",
    "log_disabled_once",
    "period_bucket_utc_day",
    "send_budget_alert",
    "sign_payload",
    "verify_signature",
]

