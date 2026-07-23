"""Human-in-the-loop approval gates exposed via ``sdk.gates``.

Public API:
    req = await sdk.gates.request(kind, agent_id, payload)
    await sdk.gates.grant(token)
    await sdk.gates.deny(token)
    granted = await sdk.gates.wait_for(request_id, timeout=300)

    @sdk.gates.require_approval(kind="patient.delete", agent_id="x", timeout=600)
    async def delete_patient(...): ...

Threat model handled:
    * Forged tokens: HMAC signature verification.
    * Replay: tokens are single-use; resolved set tracks consumed request_ids.
    * Tampering with action_hash: hmac binds request_id+action_hash+expires_at.
    * Race between two grants: per-module asyncio.Lock serializes resolution.
    * Self-approval: NOT prevented in-SDK. There is no built-in approver-
      identity gate; the same principal can request and resolve an approval.
      Route approval to a human outside the agent's process (operator
      responsibility). See the Threat Model.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import inspect
import json
import os
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, ParamSpec, TypeVar
from uuid import UUID, uuid4

import structlog

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from .errors import (
    ApprovalDenied,
    ApprovalPending,
    ApprovalTimeout,
    ApprovalTokenError,
)
from .models import ApprovalRequest
from .store import GatesStore, InMemoryGatesStore
from .tokens import make_token, parse_token

if TYPE_CHECKING:  # pragma: no cover — import only for type checking
    from ..platform.client import PlatformClient

logger = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_EXPIRES_IN = timedelta(hours=1)
MIN_GATES_SECRET_BYTES = 32
MAX_PAYLOAD_BYTES = 64 * 1024

# v0.6.2-followup — downgrade-safe default for v2 tokens.
#
# The v2 (rotation-aware, key-fingerprint-embedded) token format is the
# default. Minting v1 by default was a v0.6.2 rolling-deploy accommodation
# so a still-running v0.6.1 pod could parse tokens minted by an adjacent
# v0.6.2 pod, but those windows closed long ago. Keeping the v1 default
# would also reject freshly minted tokens once the v1 sunset below passes.
# v1 VERIFICATION stays supported for genuinely old pre-v0.6.2 tokens, up
# to that sunset.
DEFAULT_ENABLE_V2_TOKENS = True

# v0.6.2 release date + 90 days. Beyond this cutoff, legacy v1 tokens
# are REJECTED with TokenVersionTooOldError (closes the "v1 forgery
# works forever if the secret ever leaks" gap). Operators can override
# via GOVERNANCE_GATES_ACCEPT_V1_UNTIL=<ISO date> for disaster-recovery.
# The override is logged at construction time so post-hoc audits can
# see it was applied.
_V062_RELEASE_DATE = datetime(2026, 4, 17, tzinfo=timezone.utc)
DEFAULT_ACCEPT_V1_UNTIL = _V062_RELEASE_DATE + timedelta(days=90)


def _hash_action_payload(value: Any) -> str:
    """Stable SHA-256 hash of the action payload, capped at 64 KiB."""
    try:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        serialized = repr(value)
    data = serialized.encode("utf-8")[:MAX_PAYLOAD_BYTES]
    return hashlib.sha256(data).hexdigest()


# v0.7.1 platform-bridge poll cadence. Derived from the SDK <-> platform
# contract (spec §3):
#     "Poll interval: 2s jittered (randomised +/-500ms per tick),
#      capped at 10s via exponential backoff on errors."
#
# These constants are module-level (not class-level) so tests can
# monkey-patch them without subclassing GatesModule. Kept here rather
# than in platform/client.py because they describe CALLER policy (how
# often the gates module asks the platform) rather than TRANSPORT
# policy (how the platform client retries a single request).
_PLATFORM_POLL_BASE_SECONDS: float = 2.0
_PLATFORM_POLL_JITTER_SECONDS: float = 0.5
_PLATFORM_POLL_MAX_SECONDS: float = 10.0

# v0.7.1 pass-2 edge case E: after this many consecutive 5xx / network
# errors from the platform poll, the backoff "latches" at the 10s cap
# for ``_PLATFORM_POLL_LATCH_SECONDS`` before allowing a retry. Prevents
# the SDK from hammering a stuck upstream once the exponential ladder
# has already reached the cap. The latch window resets on any
# successful poll.
_PLATFORM_POLL_LATCH_THRESHOLD: int = 5
_PLATFORM_POLL_LATCH_SECONDS: float = 60.0


def _jittered_interval(base: float) -> float:
    """Return ``base`` + uniform jitter in [-0.5s, +0.5s], clamped to [0, 10s].

    Uses a simple uniform jitter — the spec is explicit that +-500ms is
    sufficient; we do NOT layer a second randomisation on top because
    each poll already has its own schedule and a deterministic cap.
    """
    delta = random.uniform(
        -_PLATFORM_POLL_JITTER_SECONDS, _PLATFORM_POLL_JITTER_SECONDS
    )
    return max(0.0, min(_PLATFORM_POLL_MAX_SECONDS, base + delta))


class GatesModule:
    """Human-in-the-loop approval gates."""

    def __init__(
        self,
        audit: AuditModule,
        *,
        secret: bytes,
        default_expires_in: timedelta = DEFAULT_EXPIRES_IN,
        store: GatesStore | None = None,
        poll_interval_s: float = 0.5,
        enable_v2_tokens: bool | None = None,
        accept_v1_until: datetime | None = None,
        platform_client: "PlatformClient | None" = None,
    ) -> None:
        from ..audit.module import _check_secret_strength

        _check_secret_strength(secret, "gates secret")
        self._audit = audit
        self._secret = secret
        self._default_expires_in = default_expires_in
        self._store: GatesStore = store or InMemoryGatesStore()
        self._poll_interval_s = poll_interval_s
        # v0.7.1 platform-bridge hook. When wired, ``request()`` dual-
        # writes the gate creation to the platform and ``wait_for()``
        # polls the platform's resolution endpoint alongside the local
        # store. Default None preserves v0.7.0 semantics exactly.
        # CLAUDE.md invariant #1: every platform interaction is
        # best-effort — the local store remains authoritative for
        # wait_for's return value and a platform outage never blocks
        # gate creation or resolution polling.
        self._platform_client: "PlatformClient | None" = platform_client
        # v0.6.2-followup: downgrade-safe token format.
        # Env var is read here (not at module import time) per the
        # project memory rule against module-level os.environ capture
        # (feedback_no_module_level_env_capture.md). Constructor arg
        # wins; env var is the deployment escape hatch.
        if enable_v2_tokens is None:
            env_flag = os.environ.get(
                "GOVERNANCE_GATES_ENABLE_V2_TOKENS", ""
            ).strip().lower()
            if env_flag in ("1", "true", "yes"):
                enable_v2_tokens = True
                logger.info(
                    "gates.v2_tokens_enabled_via_env",
                    detail=(
                        "GOVERNANCE_GATES_ENABLE_V2_TOKENS=true — "
                        "minting v2 (rotation-aware) tokens. Ensure "
                        "all deployed pods are on v0.6.2+."
                    ),
                )
            else:
                enable_v2_tokens = DEFAULT_ENABLE_V2_TOKENS
        self._enable_v2_tokens = enable_v2_tokens
        # v1 sunset. Constructor arg > env var > default-90d-post-v0.6.2.
        if accept_v1_until is None:
            env_until = os.environ.get(
                "GOVERNANCE_GATES_ACCEPT_V1_UNTIL", ""
            ).strip()
            if env_until:
                try:
                    parsed = datetime.fromisoformat(env_until)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    accept_v1_until = parsed
                    logger.warning(
                        "gates.v1_sunset_overridden",
                        new_sunset=parsed.isoformat(),
                        default_sunset=DEFAULT_ACCEPT_V1_UNTIL.isoformat(),
                        detail=(
                            "GOVERNANCE_GATES_ACCEPT_V1_UNTIL override "
                            "active — v1 legacy tokens accepted past "
                            "the default cutoff. Grants issued during "
                            "this window are still single-use and HMAC-"
                            "verified; the override only delays the "
                            "TokenVersionTooOldError gate."
                        ),
                    )
                except ValueError:
                    logger.error(
                        "gates.v1_sunset_override_malformed",
                        raw_value=env_until,
                        detail=(
                            "GOVERNANCE_GATES_ACCEPT_V1_UNTIL is not "
                            "parseable as ISO-8601 — falling back to "
                            "default sunset."
                        ),
                    )
                    accept_v1_until = DEFAULT_ACCEPT_V1_UNTIL
            else:
                accept_v1_until = DEFAULT_ACCEPT_V1_UNTIL
        self._accept_v1_until = accept_v1_until
        # Optional reference to PresenceModule for the v0.5.4 halt switch.
        # Wired by GovernanceSDK after construction via set_presence_module().
        # If None, the agent-facing gate methods (request / wait_for) run
        # without a halt check (back-compat). Operator-facing methods
        # (grant / deny / _resolve) DELIBERATELY skip the halt check so that
        # operators can still resolve gates issued BEFORE the halt — the
        # reviewer, not the agent, is the principal for grant/deny.
        self._presence: Any = None

    def set_presence_module(self, presence: Any) -> None:
        """Wire the PresenceModule for halt-switch enforcement (v0.6.2 P0).

        Called by GovernanceSDK during construction after both modules exist.
        Once set, ``request()`` — the agent-driven entry point that mints a new
        approval — fails closed with ``AgentHaltedError`` for a halted agent,
        so a halted agent cannot open new approval requests.

        ``wait_for()`` does NOT re-check the halt on each poll: it only awaits
        resolution of an ALREADY-issued request (it holds a ``request_id``, not
        the ``agent_id``), and the agent's post-approval action is separately
        halt-gated by scope/cost and the wrapped LLM clients, so a halted agent
        gains nothing by awaiting a pending gate.

        ``grant()`` and ``deny()`` intentionally DO NOT call
        ``assert_not_halted``: those are operator-facing and need to remain
        usable on gates that were issued BEFORE the halt so that pending
        human-review work can still be resolved. The reviewer is the
        principal for grant/deny, not the halted agent.
        """
        self._presence = presence

    async def close(self) -> None:
        await self._store.close()

    async def request(
        self,
        kind: str,
        agent_id: str,
        payload: Any | None = None,
        *,
        expires_in: timedelta | None = None,
    ) -> ApprovalRequest:
        """Open an approval request and return the pending object.

        Observation surface: never raises on internal failure. If the store
        is unreachable, a logged warning is emitted and the request is
        returned anyway with no persistence — the host call continues.
        Resolution will fail (unknown request_id) until the store recovers.

        The caller is responsible for surfacing the request to a human (UI,
        Slack, email, etc.). The human's tool calls ``grant(token)`` or
        ``deny(token)`` with the token field of the returned request.

        Raises:
            AgentHaltedError: the agent has been halted by an operator.
                Fail-closed before any token is minted so halted agents cannot
                keep issuing new HITL gates (v0.6.2 P0).
        """
        # v0.6.2 P0 halt switch — first thing in the agent-facing gate path.
        # If presence is wired, block halted agents from minting new approval
        # tokens. grant/deny deliberately skip this check — see set_presence_module.
        if self._presence is not None:
            await self._presence.assert_not_halted(agent_id)

        request_id = uuid4()
        action_hash = _hash_action_payload(payload)
        expires_at = datetime.now(timezone.utc) + (
            expires_in or self._default_expires_in
        )
        token = make_token(
            secret=self._secret,
            request_id=request_id,
            action_hash=action_hash,
            expires_at=expires_at,
            use_v2=self._enable_v2_tokens,
        )
        req = ApprovalRequest(
            request_id=request_id,
            agent_id=agent_id,
            kind=kind,
            action_hash=action_hash,
            expires_at=expires_at,
            payload=payload if isinstance(payload, dict) else {},
            token=token,
        )
        try:
            await self._store.insert_pending(req)
        except Exception as exc:  # noqa: BLE001 - non-breaking guarantee
            logger.error(
                "gates.request_persist_failed",
                error_type=type(exc).__name__,
                request_id=str(request_id),
            )
        # audit.log is itself non-breaking; safe to call.
        await self._audit.log(
            AuditEvent(
                agent_id=agent_id,
                kind="approval.requested",
                metadata={
                    "request_id": str(request_id),
                    "approval_kind": kind,
                    "action_hash": action_hash,
                },
            )
        )
        # v0.7.1 platform-bridge write path. Fire-and-forget — mirrors
        # the audit-event bridge contract (invariant #1). The local
        # store is already the source of truth; a platform outage here
        # cannot fail gate creation. The PlatformClient's own
        # forward_gate_request never raises.
        if self._platform_client is not None:
            try:
                self._platform_client.forward_gate_request(req)
            except Exception as exc:  # noqa: BLE001 — non-breaking
                logger.warning(
                    "gates.platform_forward_failed",
                    error_type=type(exc).__name__,
                    request_id=str(request_id),
                )
        return req

    async def grant(self, token: str) -> None:
        """Approve the request bound to ``token``. Single-use, time-bound.

        Operator-facing call: raises ApprovalTokenError on bad token, replay,
        action_hash mismatch, expiration, etc. Operators are expected to
        handle the error (e.g. show "this approval was already used").
        """
        await self._resolve(token, granted=True)

    async def deny(self, token: str) -> None:
        """Deny the request bound to ``token``. Single-use, time-bound."""
        await self._resolve(token, granted=False)

    async def _resolve(self, token: str, *, granted: bool) -> None:
        request_id, action_hash, _expires = parse_token(
            secret=self._secret,
            token=token,
            accept_v1_until=self._accept_v1_until,
        )
        # Verify action_hash by re-fetching the pending row. Defense in
        # depth: parse_token already HMAC-binds the action_hash, but we
        # cross-check against the persisted copy so a swapped hash in
        # the pending row (via a compromised DB path) still aborts the
        # resolve. compare_digest keeps crypto-hygiene consistent with
        # the audit chain.
        pending = await self._store.get_pending(request_id)
        if pending is not None and not hmac.compare_digest(
            pending.action_hash, action_hash
        ):
            raise ApprovalTokenError(
                "approval token: action_hash mismatch"
            )
        # v0.6.2 P0 "atomicity" (actually: SERIALIZED, window-tight).
        # Pre-fix ordering was:
        #     1. self._store.resolve(...)   ← DB commit A
        #     2. await self._audit.log(...) ← separate DB round-trip B
        # A worker crash between the two left the gate resolved WITHOUT
        # an audit row — Article 12 export silently missing the
        # decision. Anti-pattern called out in .agents/swe.md as
        # "'Check + write audit' pairs outside a single transaction".
        #
        # Fix: run the audit write inside an ``on_commit`` hook supplied
        # to ``resolve``. The store executes the hook inside its atomic
        # unit (asyncio lock for InMemory, engine.begin() txn for
        # Postgres) BEFORE persisting the resolution. If the hook raises,
        # the resolution is NOT persisted and the pending row stays
        # intact so the operator can retry with the same token.
        #
        # v0.6.2-followup residual risk (KNOWN, DOCUMENTED, TESTED):
        # The audit write goes through ``AuditModule.log`` which uses
        # the AUDIT engine's ``insert_with_chain_lock`` — a real DB
        # round-trip that commits before returning. This is good: the
        # audit row is durable before ``on_commit`` returns. HOWEVER,
        # gates and audit run on SEPARATE engines with SEPARATE txns.
        # The remaining narrow window is:
        #
        #     audit txn COMMITs  ← audit row durable
        #     ... <network blip, OOM, pod SIGKILL> ...
        #     gates txn COMMIT fails at engine.begin() __aexit__
        #
        # Post-fix window = one DB round-trip on a healthy connection
        # (vs pre-fix window of "BatchingWriter queue depth + 100ms
        # flush interval"). ~4 orders of magnitude narrower. The reverse
        # failure produces a GHOST audit row (approval.granted logged)
        # for a gate that stayed pending. A retry with the same token
        # will succeed and emit a SECOND audit row — the chain will
        # record two ``approval.granted`` events for one request_id.
        # Operators detecting this pattern in the chain should
        # investigate the adjacent session for a failed commit event.
        # See tests/gates/test_atomicity_engine_rollback.py for the
        # characterization test documenting this exact window.
        async def _emit_audit(req: ApprovalRequest) -> None:
            await self._audit.log(
                AuditEvent(
                    agent_id=req.agent_id,
                    kind="approval.granted" if granted else "approval.denied",
                    metadata={
                        "request_id": str(request_id),
                        "approval_kind": req.kind,
                    },
                )
            )

        await self._store.resolve(
            request_id,
            "granted" if granted else "denied",
            on_commit=_emit_audit,
        )

        # v0.7.1 pass-2 edge case B: reverse-sync the resolution to the
        # platform so the /app/approvals inbox doesn't leave a stale
        # pending card. Best-effort: the platform client filters on its
        # internal ``_forwarded_request_ids`` set so we only notify for
        # gates the platform has actually seen — a stale CLI grant
        # against a platform-down-at-creation-time gate skips silently.
        # Never raises, never blocks: the caller's grant/deny has
        # already committed locally.
        if self._platform_client is not None:
            try:
                self._platform_client.notify_local_resolution(
                    request_id,
                    "granted" if granted else "denied",
                    datetime.now(timezone.utc),
                )
            except Exception as exc:  # noqa: BLE001 — non-breaking
                logger.warning(
                    "gates.platform_reverse_sync_failed",
                    error_type=type(exc).__name__,
                    request_id=str(request_id),
                )

    async def wait_for(self, request_id: UUID, timeout: float) -> bool:
        """Block until the request is resolved. Returns True if granted.

        Polls the store every ``poll_interval_s`` seconds. Polling is the
        source of truth — multi-process correct without LISTEN/NOTIFY.

        Raises :class:`ApprovalTimeout` if the timeout elapses without
        resolution. Raises :class:`ApprovalDenied` if the request was denied.

        When a platform bridge is wired (v0.7.1), this method ALSO
        polls the platform's ``GET /api/v1/bridge/gates/:id/resolution``
        endpoint each tick. The LOCAL store remains authoritative per
        CLAUDE.md invariant #1:

        * local ``granted`` + platform ``pending``  -> return True
        * local ``pending`` + platform ``granted``  -> sync platform
          verdict back into the local store via
          ``self._store.resolve(..., on_commit=<audit emit>)`` so any
          subsequent ``wait_for`` call sees the resolution locally too,
          then return True
        * local ``denied`` (regardless of platform) -> raise
          :class:`ApprovalDenied`
        * platform unreachable / 5xx -> exponential backoff (2s -> 4s
          -> 8s -> 10s cap), keep polling local. Gate still expires at
          ``timeout`` just like the v0.7.0 contract.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        # Platform-poll backoff state. Reset to the default base on any
        # successful poll; exponentiated on any raised error up to the
        # 10s cap. Kept out of self because wait_for may be called
        # concurrently for different request_ids.
        platform_backoff = _PLATFORM_POLL_BASE_SECONDS
        next_platform_poll_at: float = loop.time()
        # Edge case E: consecutive-5xx latch. After
        # ``_PLATFORM_POLL_LATCH_THRESHOLD`` failures in a row, we hold
        # the next poll off by ``_PLATFORM_POLL_LATCH_SECONDS`` instead
        # of the normal jittered interval. Resets to 0 on any successful
        # poll. ``next_platform_poll_at`` already enforces the hold via
        # wall-clock, so this is purely state-tracking.
        platform_consecutive_failures = 0
        while True:
            # -- Local read first (authoritative per invariant #1) ---
            try:
                resolution = await self._store.get_resolution(request_id)
            except Exception as exc:
                logger.warning(
                    "gates.wait_for_poll_failed",
                    error_type=type(exc).__name__,
                    request_id=str(request_id),
                )
                resolution = None
            if resolution == "granted":
                return True
            if resolution == "denied":
                raise ApprovalDenied(
                    f"approval denied for request {request_id}"
                )
            # -- Platform read (advisory; only consulted when local is
            # still pending). Local-always-wins is enforced by the
            # ordering above: we NEVER reach this branch with a
            # non-None local resolution. -----------------------------
            if (
                self._platform_client is not None
                and loop.time() >= next_platform_poll_at
            ):
                try:
                    platform_res = (
                        await self._platform_client.poll_gate_resolution(
                            request_id
                        )
                    )
                    # Successful poll resets both the backoff ladder
                    # AND the consecutive-failure latch counter.
                    platform_backoff = _PLATFORM_POLL_BASE_SECONDS
                    platform_consecutive_failures = 0
                except Exception as exc:  # noqa: BLE001 — non-breaking
                    # DEBUG, not WARN: a platform outage shouldn't spam
                    # the log once per poll tick across every
                    # outstanding gate. The PlatformClient already
                    # bumps ``gate_polls_failed`` for observability.
                    logger.debug(
                        "gates.wait_for_platform_poll_failed",
                        error_type=type(exc).__name__,
                        request_id=str(request_id),
                    )
                    platform_res = None
                    platform_consecutive_failures += 1
                    # Exponential backoff up to 10s cap.
                    platform_backoff = min(
                        platform_backoff * 2,
                        _PLATFORM_POLL_MAX_SECONDS,
                    )
                # Edge case E: consecutive-5xx latch. Once we've been
                # at the cap long enough (5 failures in a row), hold
                # the next poll off by 60s instead of jittering at 10s.
                # This prevents hammering a stuck upstream — the local
                # store still wins, and a long-timeout wait_for will
                # eventually succeed or ApprovalTimeout at the
                # user-supplied deadline. The jittered interval
                # continues to govern the sub-latch window so a healthy
                # platform recovers immediately.
                if (
                    platform_consecutive_failures
                    >= _PLATFORM_POLL_LATCH_THRESHOLD
                ):
                    hold = _PLATFORM_POLL_LATCH_SECONDS
                else:
                    hold = _jittered_interval(platform_backoff)
                # Schedule the next platform poll by wall-clock, so a
                # faster local poll_interval_s doesn't spam the platform.
                next_platform_poll_at = loop.time() + hold
                if platform_res is not None and platform_res.status in (
                    "granted",
                    "denied",
                ):
                    await self._sync_platform_resolution(
                        request_id, platform_res.status
                    )
                    if platform_res.status == "granted":
                        return True
                    raise ApprovalDenied(
                        f"approval denied for request {request_id}"
                    )
            now = loop.time()
            if now >= deadline:
                # TODO(future): notify the platform bridge with a "timed_out"
                # resolution here so the UI card transitions to "Expired"
                # immediately instead of waiting for the gate's own expires_at
                # (up to 1 hour away). Needs further research before
                # implementing — three blockers identified in the v0.7.2
                # team review: (1) platform Zod + Postgres schema rejects
                # "timed_out" today; (2) principal-model violation — the
                # agent process writing a terminal gate state can race and
                # silently drop a legitimate operator approval; (3) no
                # opt-out mechanism (violates CLAUDE.md opt-in invariant).
                # Also missing: local approval.timed_out audit event for
                # EU AI Act Article 12 compliance. Do not implement without
                # resolving all three and a full DA + Security sign-off.
                raise ApprovalTimeout(
                    f"approval timeout after {timeout}s for request {request_id}"
                )
            # ±20% jitter on the local poll interval to avoid thundering
            # herd. The platform poll cadence is managed separately via
            # next_platform_poll_at above (2s base +- 500ms, 10s cap).
            jittered = self._poll_interval_s * random.uniform(0.8, 1.2)
            sleep_for = min(jittered, deadline - now)
            await asyncio.sleep(sleep_for)

    async def _sync_platform_resolution(
        self,
        request_id: UUID,
        status: str,
    ) -> None:
        """Commit a platform-decided verdict to the local store.

        Called from :meth:`wait_for` when the platform returns a terminal
        verdict while the local store is still pending. Uses the
        on_commit hook so the audit row lands atomically with the state
        flip (v0.6.2 P0 atomicity contract — same as ``grant``/``deny``
        but with ``by=platform`` metadata).

        Best-effort: any failure is logged and swallowed. The caller
        may still return the platform verdict to the agent because the
        agent cares about the decision, not which store won the race.
        A subsequent ``wait_for`` from a sibling process will either
        see a local resolution (this call succeeded) or repeat the
        platform poll (still pending locally) — both are correct.
        """
        try:
            pending = await self._store.get_pending(request_id)
        except Exception as exc:  # noqa: BLE001 — invariant #1
            logger.warning(
                "gates.platform_sync_get_pending_failed",
                error_type=type(exc).__name__,
                request_id=str(request_id),
            )
            return
        if pending is None:
            # Local row is gone (expired, purged, or a sibling resolved
            # it racily). Nothing to sync. Return — the caller will
            # honor the platform verdict on this call and the next
            # wait_for will re-evaluate from scratch.
            return

        async def _emit_audit(req: ApprovalRequest) -> None:
            await self._audit.log(
                AuditEvent(
                    agent_id=req.agent_id,
                    kind=(
                        "approval.granted"
                        if status == "granted"
                        else "approval.denied"
                    ),
                    metadata={
                        "request_id": str(request_id),
                        "approval_kind": req.kind,
                        # ``by=platform`` marks this resolution as
                        # originating from the platform bridge (rather
                        # than from the CLI / webhook / grant-token
                        # path). Compliance exports and retrospectives
                        # can filter on it.
                        "by": "platform",
                    },
                )
            )

        try:
            await self._store.resolve(
                request_id,
                "granted" if status == "granted" else "denied",
                on_commit=_emit_audit,
            )
        except ApprovalTokenError:
            # Race: a concurrent grant/deny (CLI or another process)
            # resolved the gate between our get_resolution() call and
            # here. That other resolution is the authoritative one; we
            # drop the platform sync on the floor. The caller still
            # honors the platform verdict this call, and the next
            # wait_for will see the local resolution directly.
            logger.info(
                "gates.platform_sync_lost_race",
                request_id=str(request_id),
            )
        except Exception as exc:  # noqa: BLE001 — invariant #1
            logger.warning(
                "gates.platform_sync_resolve_failed",
                error_type=type(exc).__name__,
                request_id=str(request_id),
            )

    def require_approval(
        self,
        *,
        kind: str,
        agent_id: str,
        timeout: float = 600.0,
        blocking: bool = True,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """Decorator: open an approval request before running the function.

        If ``blocking=True`` (default), the wrapped call blocks up to
        ``timeout`` seconds waiting for grant/deny.
        If ``blocking=False``, the wrapped call raises :class:`ApprovalPending`
        immediately and the caller resumes by calling the function again
        after a human has resolved the request.
        """

        def decorator(
            func: Callable[P, Awaitable[R]],
        ) -> Callable[P, Awaitable[R]]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"@gates.require_approval requires an async function; "
                    f"{func.__name__} is sync."
                )

            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                payload = {"args": list(args), "kwargs": dict(kwargs)}
                req = await self.request(
                    kind=kind, agent_id=agent_id, payload=payload
                )
                if not blocking:
                    raise ApprovalPending(str(req.request_id))
                await self.wait_for(req.request_id, timeout=timeout)
                return await func(*args, **kwargs)

            return wrapper

        return decorator
