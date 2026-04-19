"""Async HTTP client that forwards audit events to the platform ingest endpoint.

Design constraints (from the v0.7.0 platform-bridge spec):

1. **Never raise to the host app.** Every exception is caught inside the
   background worker. ``forward()`` is a synchronous enqueue that cannot
   raise — a full queue drops the oldest event and logs WARN.

2. **Never block the event loop.** Enqueue is O(1). The worker runs as a
   separate ``asyncio.Task`` on the same loop, but it ``await``s the
   queue, never the user's call path.

3. **Never log the bearer token.** Tokens appear only in the
   ``Authorization`` header. When an error log needs to identify a
   token, we use the first 8 hex chars of ``sha256(token)`` — enough
   to correlate with rotation events, not enough to exfiltrate.

4. **TLS verify is mandatory.** ``httpx.AsyncClient(verify=True)``. The
   one exception is explicit localhost URLs (``http://localhost:*`` /
   ``http://127.0.0.1:*``) for local development. Any ``http://`` on a
   non-localhost host is rejected at client init with ValueError —
   **not** silently upgraded to https.

5. **Retry budget is per-event.** Exponential backoff 1s → 2s → 4s → 8s,
   capped at ~15s cumulative delay per event. After the budget is
   exhausted the event is dropped and a cumulative drop counter is
   incremented.

6. **Graceful close.** ``close()`` flushes pending forwards with a 5s
   deadline, then cancels the worker. Dropped counts are logged once.

7. **401 disables the bridge.** If the platform rejects our token we
   log WARN (with sha256-prefix token identifier) and flip the bridge
   to permanently-disabled for the lifetime of this client instance.
   Host audit.log() continues on local-only storage.

Not in scope for v0.7.0:
    * Batching (one POST per event). v0.8 will introduce batched ingest.
    * Circuit breaker. The 5xx backoff + cumulative-drop WARN gives
      operators enough signal to intervene without machinery that can
      itself fail-closed on a transient blip.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

from .config import PlatformConfig
from .errors import PlatformConfigError

if TYPE_CHECKING:  # pragma: no cover — import only for type checking
    from ..audit.models import AuditEventRecord


logger = structlog.get_logger(__name__)

# Per-event cumulative retry budget. Summed backoff of 1+2+4+8 = 15s
# matches the spec; a handful of milliseconds for the actual POST is
# included implicitly because we measure elapsed wall-clock, not just
# sleep time.
MAX_CUMULATIVE_RETRY_SECONDS = 15.0

# Graceful-close flush deadline. After this much wall-clock, any events
# still in the queue are dropped and counted.
CLOSE_FLUSH_DEADLINE_SECONDS = 5.0

# Hourly rate-limit key for 401 WARN de-duplication. Computed as
# epoch_seconds // 3600 so we log once per (token_hash_prefix, hour).
_WARN_RATE_LIMIT_WINDOW_SECONDS = 3600

# We hold a small burst allowance on WARN-level "5xx dropped" summaries
# so a transient outage doesn't flood the log. One WARN per minute with
# cumulative drop count attached.
_FIVEXX_WARN_WINDOW_SECONDS = 60.0

def _resolve_sdk_version() -> str:
    """Resolve the SDK version from installed package metadata.

    Lazily evaluated so the bridge's User-Agent tracks the actual
    shipped version across future releases without a hardcode. DA P1-4.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("code-atelier-governance")
    except (ImportError, PackageNotFoundError):  # pragma: no cover
        return "0.0.0+unknown"


SDK_VERSION = _resolve_sdk_version()


@dataclass
class _Stats:
    """Running counters for ops. Exposed via ``PlatformClient.stats()``.

    Every counter increments monotonically for the lifetime of the
    client. Consumers can snapshot and diff across calls to compute
    rates. Reset on ``close()``? No — the SDK constructs one client
    per process, so lifetime == process.
    """

    sent: int = 0
    dropped_queue_full: int = 0
    dropped_4xx: int = 0
    dropped_5xx: int = 0
    # Incremented each time a 409 seq_out_of_order arrives AFTER the
    # single per-event self-heal has already been applied, or when the
    # platform's 409 body lacks ``expected_seq``. Surfaces parallel-SDK
    # chain conflicts that would otherwise drop silently at DEBUG.
    dropped_seq_conflict: int = 0
    retries: int = 0


@dataclass
class _QueuedEvent:
    """Internal wrapper for events in the forward queue.

    Holds the record plus the timestamp at which it was enqueued so the
    worker can enforce the 15s per-event retry budget as wall-clock
    elapsed (not just retry-attempt count).
    """

    event: "AuditEventRecord"
    enqueued_at: float = field(default_factory=time.monotonic)


def _token_fingerprint(token: str) -> str:
    """Return a stable non-reversible identifier for a bearer token.

    sha256 digest truncated to 8 hex chars. Long enough to disambiguate
    between multiple tokens in a fleet during rotation; short enough
    that a log leak does not meaningfully weaken the pre-image
    resistance of the underlying token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


_BLOCKED_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / OpenStack / DigitalOcean
        "metadata.google.internal",
        "metadata",
        "metadata.azure.com",
    }
)


def _host_is_private_ip(host: str) -> bool:
    """True iff ``host`` is a literal private/loopback/link-local IP."""
    from ipaddress import ip_address

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


def _validate_url_for_tls(
    url: str, trusted_hosts: tuple[str, ...] | None = None
) -> None:
    """Validate an ingest URL at PlatformClient init.

    Three layers, applied in order:

    1. **Scheme** — ``http://`` is accepted only for localhost; anything
       else must be ``https://``.
    2. **Default SSRF guard** — the URL's host is rejected if it is a
       literal private/loopback/link-local/reserved IP, or a known
       cloud-metadata host (AWS 169.254.169.254, GCP
       metadata.google.internal, Azure metadata.azure.com, etc.). This
       runs even when ``trusted_hosts`` is ``None`` so a compromised env
       var (``GOVERNANCE_PLATFORM_INGEST_URL=https://169.254.169.254/…``)
       cannot silently exfiltrate audit events to an IMDS endpoint.
       ``localhost`` / ``127.0.0.1`` / ``::1`` are exempt so local-dev
       setups still work.
    3. **Optional allowlist** — when ``trusted_hosts`` is set, the host
       must also be in the allowlist. Pins the bridge to a specific
       platform host for compliance-hardened deployments.

    Raises
    ------
    PlatformConfigError
        On any of the three failure modes. The error message names the
        failure but never echoes the allowlist (treat as sensitive).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    localhost_hosts = {"localhost", "127.0.0.1", "::1"}

    if parsed.scheme == "https":
        pass  # scheme OK, fall through
    elif parsed.scheme == "http":
        if host not in localhost_hosts:
            raise PlatformConfigError(
                f"PlatformConfig.ingest_url uses http:// with non-localhost "
                f"host {host!r}. TLS is mandatory for remote ingestion; use "
                f"https://. (Only http://localhost:* and http://127.0.0.1:* "
                f"are permitted for local development.)"
            )
    else:
        raise PlatformConfigError(
            f"PlatformConfig.ingest_url must use http:// or https:// "
            f"(got scheme {parsed.scheme!r})"
        )

    # Default SSRF guard — always runs. Only localhost is exempt.
    if host not in localhost_hosts:
        if host in _BLOCKED_METADATA_HOSTS:
            raise PlatformConfigError(
                f"PlatformConfig.ingest_url host {host!r} is a known "
                f"cloud-metadata endpoint. Refusing to POST audit events "
                f"(SSRF defence)."
            )
        if _host_is_private_ip(host):
            raise PlatformConfigError(
                f"PlatformConfig.ingest_url host {host!r} is a literal "
                f"private/loopback/link-local IP. Refusing to POST audit "
                f"events (SSRF defence). Use the public platform hostname."
            )

    if trusted_hosts is not None:
        allowed = {h.lower() for h in trusted_hosts}
        if host not in allowed:
            # Security P1: SSRF allowlist. Never echo the configured
            # tuple in the exception — a production config may consider
            # the list of valid hosts sensitive. The attempted host is
            # safe to include because the user supplied it.
            raise PlatformConfigError(
                f"PlatformConfig.ingest_url host {host!r} is not in the "
                f"configured trusted_hosts allowlist. Refusing to POST "
                f"audit events to an untrusted destination."
            )


class PlatformClient:
    """Fire-and-forget client that forwards audit events to the platform.

    Lifecycle:
        client = PlatformClient(config)              # no I/O
        await client.start()                          # spawns worker
        client.forward(record)                        # sync enqueue
        ...
        await client.close()                          # flush + cancel

    The SDK wires ``start`` / ``close`` through ``GovernanceSDK.start()``
    and ``GovernanceSDK.close()``. Callers do not interact with the
    client directly.
    """

    def __init__(self, config: PlatformConfig) -> None:
        # Fail loudly at init if httpx isn't installed. The SDK catches
        # this at its own construction and re-raises ImportError with
        # the pip-install instruction, so the user never gets a bare
        # ModuleNotFoundError from deep inside the stack.
        try:
            import httpx  # noqa: F401  — presence check
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "pip install 'code-atelier-governance[platform]' to enable "
                "platform bridge"
            ) from exc

        _validate_url_for_tls(config.ingest_url, config.trusted_hosts)

        self._config = config
        self._token_fp = _token_fingerprint(config.ingest_token)
        # Bounded queue so memory can't grow unbounded under platform
        # outage. When full, forward() drops the oldest. Using a
        # ``deque``-style drop-oldest policy via ``asyncio.Queue`` isn't
        # native — we implement it manually in ``forward``.
        self._queue: asyncio.Queue[_QueuedEvent] = asyncio.Queue(
            maxsize=config.max_queue_size
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._client: Any = None  # httpx.AsyncClient; typed Any to keep httpx optional
        self._stats = _Stats()
        # Process-scoped auth failure latch. Flipped on the first 401;
        # once flipped, forward() silently drops and the worker exits
        # after draining its current in-flight request.
        self._auth_failed = False
        # Timestamp of the last 401 WARN we emitted. Coalesced per
        # (token_fp, hour) so a rotation failure doesn't drown the log.
        self._last_auth_warn_window: int = -1
        # Timestamp of the last 5xx summary WARN we emitted. Coalesced
        # per minute so transient platform hiccups don't fill the log.
        self._last_5xx_warn_monotonic: float = 0.0
        self._pending_5xx_since_warn: int = 0
        # Queue-full drop summary: same pattern as the 5xx coalescer.
        # Without this, a 9000-event burst against a saturated queue
        # would emit 9000 WARN lines. SWE-B P1.
        self._last_queue_full_warn_monotonic: float = 0.0
        self._pending_queue_full_since_warn: int = 0
        # Seq-conflict drop summary: fires when a 409 seq_out_of_order
        # slips past the single-shot self-heal (parallel-SDK chain
        # contention, or platform omitting expected_seq). SWE-B P1.
        self._last_seq_conflict_warn_monotonic: float = 0.0
        self._pending_seq_conflict_since_warn: int = 0
        self._started = False
        self._closed = False
        # Per-workflow monotonic seq counter. Bridge serializes forwards
        # through ``_seq_lock`` + ``_next_seq`` so the platform's
        # (workflow_id, seq) unique index is satisfied. Starts at 1
        # because the platform rejects seq=0 at the Zod layer; if the
        # platform already has events for this token's workflow (e.g.
        # a prior SDK process, or a curl probe during dev), the 409
        # seq_out_of_order response carries ``expected_seq`` which we
        # use to self-heal the counter in-place.
        self._next_seq: int = 1
        self._seq_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the background worker and create the httpx client.

        Safe to call multiple times — idempotent. The SDK calls this
        from ``GovernanceSDK.start()`` after the audit module has
        started, so the first ``forward()`` can only happen after the
        worker is already draining.
        """
        if self._started:
            return
        # Deferred import: keeps httpx out of the hot import path for
        # users who have not installed the [platform] extra.
        import httpx

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._config.timeout_seconds),
            verify=True,  # TLS verification is MANDATORY — no self-signed bypass.
            # Security P0: trust_env=False disables httpx's default read of
            # HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / SSL_CERT_FILE from the
            # process environment. An attacker with env-var control (CI,
            # container escape, adjacent compromised dep) could otherwise
            # route every audit POST — bearer token, chain hashes, and
            # payloads — through a proxy they control. The SDK already
            # validates the configured URL; disabling env-proxies closes
            # the remaining exfil path.
            trust_env=False,
            # Defence-in-depth: pin the current httpx default so a future
            # version bump that flips it cannot silently open a redirect
            # exfil path (302 → attacker-controlled host).
            follow_redirects=False,
            # No custom transport — use the default which honours the
            # host's CA bundle (via certifi inside httpx).
        )
        loop = asyncio.get_running_loop()
        self._worker_task = loop.create_task(self._worker_loop())
        self._started = True

    async def close(self) -> None:
        """Flush pending forwards with a 5s deadline, then cancel the worker.

        Closing is fire-and-forget for the caller (the SDK). Any events
        still in the queue after the deadline are counted as drops and
        a single summary log line is emitted.
        """
        if self._closed:
            return
        self._closed = True
        # Signal the worker that no further items will be enqueued by
        # putting a sentinel on the queue. We use None to avoid needing
        # a separate ``shutdown`` event, which would race with the
        # queue.get() in the worker.
        try:
            await asyncio.wait_for(
                self._drain_queue(),
                timeout=CLOSE_FLUSH_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            dropped = self._queue.qsize()
            logger.warning(
                "platform.close_drain_timeout",
                dropped=dropped,
                deadline_seconds=CLOSE_FLUSH_DEADLINE_SECONDS,
                token_fp=self._token_fp,
            )
            self._stats.dropped_queue_full += dropped
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001 — close is best-effort
                logger.warning(
                    "platform.client_close_failed",
                    error_type=type(exc).__name__,
                )
            self._client = None

    async def _drain_queue(self) -> None:
        """Wait for the queue to empty by polling qsize.

        ``asyncio.Queue.join()`` would be nicer but requires every
        consumer to call ``task_done()`` exactly once. The worker
        already does that; we use join() for clarity.
        """
        await self._queue.join()

    # ------------------------------------------------------------------
    # Public fire-and-forget enqueue
    # ------------------------------------------------------------------

    def forward(self, event: "AuditEventRecord") -> None:
        """Enqueue ``event`` for background forward. Never raises, never blocks.

        Called synchronously from ``AuditModule.log()`` after the local
        write commits. The return is O(1): either a successful put or
        a drop-oldest + put.

        Four fast-path exits:
            1. Bridge auth failed earlier -> silently drop.
            2. Closed -> silently drop.
            3. Queue full -> drop oldest + WARN + count.
            4. Normal -> put and return.
        """
        if self._auth_failed or self._closed or not self._started:
            # Pre-start / post-close / auth-disabled drops are counted
            # separately from "queue full" so operators can distinguish
            # "broken config" from "platform slow."
            self._stats.dropped_queue_full += 1
            return
        queued = _QueuedEvent(event=event)
        try:
            self._queue.put_nowait(queued)
            return
        except asyncio.QueueFull:
            pass
        # Drop oldest, then retry put. Do NOT drop the new event — the
        # freshest events are the most operationally interesting, and
        # a run of drops against a saturated queue should show the
        # platform the *current* state, not the stale backlog.
        try:
            self._queue.get_nowait()
            # Mark the dropped task as done so the queue.join() in
            # close() can converge.
            self._queue.task_done()
            self._stats.dropped_queue_full += 1
            self._pending_queue_full_since_warn += 1
            # Coalesce WARNs: one per minute summarizes the full burst
            # instead of flooding the log with one line per dropped
            # event. SWE-B P1 rate-limit pattern (mirrors 5xx summary).
            now = time.monotonic()
            if (
                now - self._last_queue_full_warn_monotonic
                >= _FIVEXX_WARN_WINDOW_SECONDS
            ):
                self._last_queue_full_warn_monotonic = now
                logger.warning(
                    "platform.queue_full_drop_summary",
                    dropped_queue_full_total=self._stats.dropped_queue_full,
                    dropped_since_last_warn=self._pending_queue_full_since_warn,
                    max_queue_size=self._config.max_queue_size,
                    token_fp=self._token_fp,
                )
                self._pending_queue_full_since_warn = 0
        except asyncio.QueueEmpty:  # pragma: no cover — racy, but benign
            pass
        try:
            self._queue.put_nowait(queued)
        except asyncio.QueueFull:  # pragma: no cover — racy, counts as drop
            self._stats.dropped_queue_full += 1

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self) -> None:
        """Consume the queue, POST each event, handle errors per the spec.

        The worker is a single task because the spec says one POST per
        event and there is no benefit to parallelism at v0.7.0's
        per-SDK throughput. Concurrency can be added in v0.8 alongside
        batching.
        """
        while True:
            try:
                queued = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                if self._auth_failed:
                    # Drain silently — the bridge is disabled for the
                    # rest of the process. Still mark done so close()
                    # can converge.
                    self._stats.dropped_queue_full += 1
                    continue
                await self._forward_one(queued)
            except asyncio.CancelledError:
                # Requeue? No — per spec we drop on cancel, the caller
                # is shutting down and there is no guarantee the loop
                # will drain. Counter is bumped for observability.
                self._stats.dropped_queue_full += 1
                return
            except Exception as exc:  # noqa: BLE001 — worker MUST NOT die
                logger.warning(
                    "platform.worker_unexpected_error",
                    error_type=type(exc).__name__,
                    token_fp=self._token_fp,
                )
                self._stats.dropped_5xx += 1
            finally:
                self._queue.task_done()

    async def _forward_one(self, queued: _QueuedEvent) -> None:
        """POST one event, with retries per the spec.

        The retry loop bails out on:
            * 2xx success -> increment sent, return.
            * 4xx non-429 -> terminal drop, no retry.
            * 401         -> auth-failed latch, disable bridge.
            * 409         -> DEBUG log (duplicate / seq_out_of_order),
                             treated as success for counting purposes.
            * 429         -> honor Retry-After, retry.
            * 5xx / conn  -> exponential backoff retry.

        Cumulative wall-clock is capped at 15s. After the budget is
        exhausted or ``max_retries`` is reached, the event is dropped.
        """
        headers = {
            "Authorization": f"Bearer {self._config.ingest_token}",
            "Content-Type": "application/json",
            "User-Agent": f"codeatelier-governance/{SDK_VERSION}",
        }

        async with self._seq_lock:
            seq = self._next_seq
            self._next_seq += 1
        body = _event_to_wire(queued.event, seq=seq)

        attempt = 0
        backoff = 1.0  # seconds; doubles each retry
        start = time.monotonic()
        seq_healed = False

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= MAX_CUMULATIVE_RETRY_SECONDS:
                # Budget exhausted — terminal drop, counted as 5xx.
                self._record_5xx_drop(
                    reason="retry_budget_exhausted",
                    attempt=attempt,
                    elapsed=elapsed,
                )
                return
            if attempt > self._config.max_retries:
                self._record_5xx_drop(
                    reason="max_retries_exceeded",
                    attempt=attempt,
                    elapsed=elapsed,
                )
                return

            try:
                assert self._client is not None  # always set in start()
                response = await self._client.post(
                    self._config.ingest_url,
                    json=body,
                    headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — conn / timeout
                # Connection errors are retryable the same way 5xx is.
                self._stats.retries += 1
                if not await self._sleep_with_cap(backoff, start):
                    self._record_5xx_drop(
                        reason=f"connection_error:{type(exc).__name__}",
                        attempt=attempt,
                        elapsed=time.monotonic() - start,
                    )
                    return
                backoff *= 2
                attempt += 1
                continue

            status = response.status_code
            if 200 <= status < 300:
                self._stats.sent += 1
                return
            if status == 401:
                self._handle_auth_failure(response)
                return
            if status == 409:
                # Two flavors:
                #   * duplicate event_id — normal on retry; treat as success
                #   * seq_out_of_order — platform knows the expected seq;
                #     self-heal the counter and retry ONCE. This handles
                #     the cold-start case where the SDK's in-memory
                #     counter doesn't know about prior events on the
                #     workflow's chain.
                expected_seq = _parse_expected_seq(response)
                if (
                    expected_seq is not None
                    and not seq_healed
                    and attempt <= self._config.max_retries
                ):
                    async with self._seq_lock:
                        # Jump ahead. Use max() so concurrent forwards
                        # that already observed a higher counter don't
                        # regress.
                        if self._next_seq < expected_seq + 1:
                            self._next_seq = expected_seq + 1
                    seq = expected_seq
                    body = _event_to_wire(queued.event, seq=seq)
                    seq_healed = True
                    self._stats.retries += 1
                    attempt += 1
                    continue
                # Fallthrough: either
                #   * platform omitted expected_seq (we can't self-heal), or
                #   * self-heal was already used on this event and we're
                #     STILL getting 409 — indicates a parallel writer
                #     against the same workflow chain.
                # Previously this path logged DEBUG and dropped silently,
                # hiding real data loss in multi-SDK deployments. We now
                # bump a dedicated counter and emit a WARN summary at
                # most once per minute. SWE-B P1.
                self._record_seq_conflict_drop(
                    status=status,
                    seq=seq,
                    seq_healed=seq_healed,
                )
                return
            if status == 413:
                self._stats.dropped_4xx += 1
                logger.warning(
                    "platform.payload_too_large",
                    status=status,
                    event_id=str(queued.event.event_id),
                    token_fp=self._token_fp,
                )
                return
            if 400 <= status < 500 and status != 429:
                self._stats.dropped_4xx += 1
                logger.warning(
                    "platform.client_error",
                    status=status,
                    event_id=str(queued.event.event_id),
                    token_fp=self._token_fp,
                    # Body is scrubbed of the token; we include the
                    # platform's error reason for debuggability.
                    body_preview=_safe_body_preview(response),
                )
                return
            if status == 429:
                retry_after = _parse_retry_after(response)
                self._stats.retries += 1
                if not await self._sleep_with_cap(
                    retry_after if retry_after is not None else backoff,
                    start,
                ):
                    self._record_5xx_drop(
                        reason="retry_after_exceeds_budget",
                        attempt=attempt,
                        elapsed=time.monotonic() - start,
                    )
                    return
                if retry_after is None:
                    backoff *= 2
                attempt += 1
                continue
            # 5xx: silent retry with backoff.
            self._stats.retries += 1
            if not await self._sleep_with_cap(backoff, start):
                self._record_5xx_drop(
                    reason=f"server_error_{status}",
                    attempt=attempt,
                    elapsed=time.monotonic() - start,
                )
                return
            backoff *= 2
            attempt += 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _sleep_with_cap(self, delay: float, start: float) -> bool:
        """Sleep ``delay`` seconds unless it would exceed the retry budget.

        Returns True if the sleep completed, False if the budget would
        be exceeded (caller drops the event). We cap the sleep at
        whatever remains of the 15s budget so we never overshoot.
        """
        elapsed = time.monotonic() - start
        remaining = MAX_CUMULATIVE_RETRY_SECONDS - elapsed
        if remaining <= 0:
            return False
        await asyncio.sleep(min(delay, remaining))
        return True

    def _handle_auth_failure(self, response: Any) -> None:
        """Latch auth-failed, log WARN once per (token_fp, hour).

        Does NOT log the token or the response body (a misconfigured
        server could echo headers). The token_fp + window key give us
        enough to correlate across rotation events.
        """
        self._auth_failed = True
        self._stats.dropped_4xx += 1
        window = int(time.time() // _WARN_RATE_LIMIT_WINDOW_SECONDS)
        if window != self._last_auth_warn_window:
            self._last_auth_warn_window = window
            logger.warning(
                "platform.auth_failed_bridge_disabled",
                token_fp=self._token_fp,
                # DO NOT add response.text / headers here — bearer token
                # could be echoed. status is sufficient.
                status=response.status_code,
                detail=(
                    "Platform rejected ingest token (401). Bridge "
                    "disabled for the rest of this process. Host app "
                    "continues on local-only audit storage."
                ),
            )

    def _record_seq_conflict_drop(
        self, *, status: int, seq: int, seq_healed: bool
    ) -> None:
        """Count a 409 that bypassed the per-event self-heal.

        Emits a per-minute WARN summary mirroring the 5xx and queue-full
        coalescers. ``seq_healed`` in the log distinguishes
        "platform omitted expected_seq" (False) from "parallel writer
        caused repeat conflict" (True) so operators can diagnose.
        """
        self._stats.dropped_seq_conflict += 1
        self._pending_seq_conflict_since_warn += 1
        now = time.monotonic()
        if (
            now - self._last_seq_conflict_warn_monotonic
            >= _FIVEXX_WARN_WINDOW_SECONDS
        ):
            self._last_seq_conflict_warn_monotonic = now
            logger.warning(
                "platform.seq_conflict_drop_summary",
                dropped_seq_conflict_total=self._stats.dropped_seq_conflict,
                dropped_since_last_warn=self._pending_seq_conflict_since_warn,
                status=status,
                last_seq=seq,
                self_heal_already_used=seq_healed,
                token_fp=self._token_fp,
            )
            self._pending_seq_conflict_since_warn = 0

    def _record_5xx_drop(
        self, *, reason: str, attempt: int, elapsed: float
    ) -> None:
        """Increment 5xx counter, log a per-minute summary."""
        self._stats.dropped_5xx += 1
        self._pending_5xx_since_warn += 1
        now = time.monotonic()
        if now - self._last_5xx_warn_monotonic >= _FIVEXX_WARN_WINDOW_SECONDS:
            self._last_5xx_warn_monotonic = now
            logger.warning(
                "platform.server_side_drop_summary",
                dropped_5xx_total=self._stats.dropped_5xx,
                dropped_5xx_since_last_warn=self._pending_5xx_since_warn,
                last_reason=reason,
                last_attempt=attempt,
                last_elapsed_seconds=round(elapsed, 3),
                token_fp=self._token_fp,
            )
            self._pending_5xx_since_warn = 0

    # ------------------------------------------------------------------
    # Ops surface
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of the running counters + state flags.

        Safe to call at any time. Returned dict is a copy, so mutating
        it does not affect the client. ``disabled`` is a boolean flag
        that flips True after a 401 latch (bridge disabled for the
        process lifetime).
        """
        return {
            "sent": self._stats.sent,
            "dropped_queue_full": self._stats.dropped_queue_full,
            "dropped_4xx": self._stats.dropped_4xx,
            "dropped_5xx": self._stats.dropped_5xx,
            "dropped_seq_conflict": self._stats.dropped_seq_conflict,
            "disabled": self._auth_failed,
            "retries": self._stats.retries,
        }


# ----------------------------------------------------------------------
# Wire format helpers
# ----------------------------------------------------------------------


def _event_to_wire(event: "AuditEventRecord", *, seq: int) -> dict[str, Any]:
    """Project an AuditEventRecord onto the ingest wire format.

    Mapping (from spec):
        event_id      -> str(record.event_id)
        event_type    -> record.kind
        agent_id      -> record.agent_id
        seq           -> caller-supplied monotonic counter; platform
                         enforces ``seq == last_seq + 1`` under advisory
                         lock per (workflow_id). Caller (PlatformClient)
                         maintains the counter with a 409-seq-out-of-order
                         self-heal hook.
        decision      -> inferred from record.kind (allow/deny/None)
        payload       -> full model dump with chain fields included
        prev_hash     -> record.prev_hash (or "" for the root event)
        client_hmac   -> record.hmac
        sdk_version   -> resolved from importlib.metadata
    """
    decision = _infer_decision(event.kind)
    # Payload mirrors the AuditEventRecord as JSON-safe primitives.
    # We include every chain field so the platform can verify the HMAC
    # itself without another round-trip. Signature fields are included
    # when present (for F6 Ed25519 rows).
    payload: dict[str, Any] = {
        "session_id": str(event.session_id),
        "kind": event.kind,
        "created_at": event.created_at.isoformat(),
        "metadata": event.metadata,
    }
    if event.parent_event_id is not None:
        payload["parent_event_id"] = str(event.parent_event_id)
    if event.model is not None:
        payload["model"] = event.model
    if event.input_hash is not None:
        payload["input_hash"] = event.input_hash
    if event.output_hash is not None:
        payload["output_hash"] = event.output_hash
    if event.signing_key_fingerprint is not None:
        payload["signing_key_fingerprint"] = event.signing_key_fingerprint
    payload["signature_status"] = event.signature_status
    # Raw bytes signature is hex-encoded to survive JSON. Absent -> None.
    if event.signature is not None:
        payload["signature_hex"] = event.signature.hex()

    wire: dict[str, Any] = {
        "event_id": str(event.event_id),
        "event_type": event.kind,
        "agent_id": event.agent_id,
        "seq": seq,
        "payload": payload,
        "prev_hash": event.prev_hash or "",
        "client_hmac": event.hmac,
        "sdk_version": SDK_VERSION,
    }
    # Platform's Zod schema treats ``decision`` as optional-string, not
    # nullable-string. Send the key only when we have a value, otherwise
    # omit entirely so the field is undefined on the wire.
    if decision is not None:
        wire["decision"] = decision
    return wire


_DENY_SEGMENTS = {"denied", "violation", "exceeded", "halted"}
_ALLOW_SEGMENTS = {"granted", "allowed"}


def _infer_decision(kind: str) -> str | None:
    """Map an event kind to allow / deny / None.

    The ingest schema requires ``decision`` to be one of
    {"allow", "deny", null}. We infer from the event kind because the
    SDK's AuditEventRecord doesn't store decision directly — enforcement
    modules write distinct kinds for allow vs deny outcomes.

    Matches on dot-segments (not just suffix) so compound kinds like
    ``approval.granted.batch`` still resolve to ``"allow"``. DA P0-1.
    """
    segments = set(kind.split("."))
    if segments & _DENY_SEGMENTS:
        return "deny"
    if segments & _ALLOW_SEGMENTS:
        return "allow"
    return None


_EXPECTED_SEQ_RE = re.compile(r"expected seq=(\d+)")


def _parse_expected_seq(response: Any) -> int | None:
    """Extract ``expected_seq`` from a 409 seq_out_of_order response.

    Platform returns shape:
        {"ok": false, "error": "seq_out_of_order",
         "message": "expected seq=42, got 17"}

    We regex the message so a minor wording drift on the platform side
    (e.g. adding spaces) doesn't break self-heal — but if the platform
    stops returning the number entirely, we return None and fall back
    to the classic "log DEBUG and drop" behavior.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict):
        return None
    if body.get("error") != "seq_out_of_order":
        return None
    msg = body.get("message", "")
    if not isinstance(msg, str):
        return None
    match = _EXPECTED_SEQ_RE.search(msg)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _parse_retry_after(response: Any) -> float | None:
    """Return the Retry-After header as seconds, or None if absent/unparseable.

    Supports the integer-seconds form (e.g. ``Retry-After: 30``). We
    deliberately do NOT support the HTTP-date form because it's rare in
    API responses and its parsing surface is a known source of bugs
    (timezone handling, locale-dependent month names). On an unparseable
    value we fall back to exponential backoff.
    """
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        seconds = float(header)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds


# Matches any ``Bearer <token>`` substring a misconfigured reverse-proxy
# might echo into a 4xx HTML body. Replaced before we log anything at
# WARN. DA P0-2.
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+\-/=]+", re.IGNORECASE)


def _safe_body_preview(response: Any, max_chars: int = 256) -> str:
    """Return a bounded, bearer-scrubbed preview of the response body.

    Truncates to ``max_chars`` so a misbehaving server can't blow up
    our log line. Scrubs any ``Bearer <token>`` substring in case the
    platform (or an intermediary) echoes the Authorization header into
    an error page. Never includes response headers (they're even more
    likely to carry the token verbatim).
    """
    try:
        text = str(response.text)
    except Exception:  # noqa: BLE001
        return "<unavailable>"
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text
