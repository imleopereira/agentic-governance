"""Configuration dataclass for the platform bridge.

The PlatformConfig is constructed by GovernanceSDK at init time from the
three GovernanceConfig fields (``platform_ingest_url``,
``platform_ingest_token``, ``platform_bridge_enabled``) plus the matching
env-var fallbacks resolved inside ``GovernanceSDK.__init__``.

Callers should not construct PlatformConfig directly in application code;
the SDK owns its lifecycle. It is exported from
``codeatelier_governance.platform`` only so advanced integrators and test
fixtures can build a PlatformClient in isolation.

Security-critical fields:
    ingest_token -- opaque bearer. NEVER log this. Any diagnostic that
                    needs to identify a token should use
                    ``hashlib.sha256(token.encode()).hexdigest()[:8]``
                    as the identifier.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformConfig:
    """Immutable configuration for the platform audit-ingest bridge.

    Parameters
    ----------
    ingest_url:
        Full URL to the platform's ingest endpoint, e.g.
        ``https://codeatelier.tech/api/v1/ingest/events``. Must be https
        unless the host is explicitly localhost (127.0.0.1 or
        ``localhost``) for local development. TLS verification is
        ALWAYS on — no self-signed bypass, even in dev.
    ingest_token:
        Opaque bearer credential provisioned by the platform. Sent in
        the ``Authorization: Bearer <token>`` header on every POST.
        NEVER logged. On 401 the bridge disables itself for the
        remaining process lifetime.
    enabled:
        Master switch. False means "SDK knows about the bridge but
        will not instantiate it." Mirrors the
        ``platform_bridge_enabled`` field of ``GovernanceConfig``.
    max_queue_size:
        Upper bound on the in-process forward queue. When full, the
        oldest event is dropped (see ``PlatformClient.forward``).
        Default 1000 matches the spec. Tune down for memory-bounded
        hosts; tune up for high-throughput agents with bursty traffic.
    max_retries:
        Number of retry attempts on 429 / 5xx / connection errors.
        Default 4 — with 1s-2s-4s-8s backoff that sums to 15s of
        cumulative delay, matching the per-event retry budget from the
        spec.
    timeout_seconds:
        Per-request timeout on the httpx POST. Defaults to 10s to match
        a realistic ingress latency ceiling; individual retries still
        observe this budget.
    """

    ingest_url: str
    ingest_token: str
    enabled: bool = True
    max_queue_size: int = 1000
    max_retries: int = 4
    timeout_seconds: float = 10.0
    # Optional SSRF allowlist: if set, PlatformClient refuses any
    # ingest_url whose hostname is not in this tuple. Used by security-
    # hardened deployments that pin the bridge to a known set of
    # platform hostnames. None = "no allowlist, any valid TLS host
    # accepted" (still subject to http:// rejection in client.py).
    # Security P1.
    trusted_hosts: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        # SWE-B P2: strip trailing/leading whitespace that commonly slips
        # in via env-var copy-paste (``\n`` from a shell heredoc, the
        # stray space at the end of a .env line). frozen=True blocks
        # normal assignment so we route through object.__setattr__.
        object.__setattr__(self, "ingest_url", self.ingest_url.strip())
        object.__setattr__(self, "ingest_token", self.ingest_token.strip())
        if not self.ingest_url:
            raise ValueError("PlatformConfig.ingest_url must be non-empty")
        if not self.ingest_token:
            raise ValueError("PlatformConfig.ingest_token must be non-empty")
        if self.max_queue_size < 1:
            raise ValueError(
                f"PlatformConfig.max_queue_size must be >= 1 "
                f"(got {self.max_queue_size})"
            )
        if self.max_retries < 0:
            raise ValueError(
                f"PlatformConfig.max_retries must be >= 0 "
                f"(got {self.max_retries})"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"PlatformConfig.timeout_seconds must be > 0 "
                f"(got {self.timeout_seconds})"
            )
