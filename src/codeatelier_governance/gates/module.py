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
    * Self-approval: tokens have to come from outside the agent's process —
      v0.1 documents this as an operator responsibility (no built-in identity
      gate; pluggable in v0.2).
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
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar
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

logger = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_EXPIRES_IN = timedelta(hours=1)
MIN_GATES_SECRET_BYTES = 32
MAX_PAYLOAD_BYTES = 64 * 1024

# v0.6.2-followup — downgrade-safe default for v2 tokens.
#
# v0.6.2 introduced the v2 (rotation-aware, key-fingerprint-embedded)
# token format. Minting v2 by default breaks rolling-deploy scenarios
# where a v0.6.1 pod is still handling grant/deny for tokens minted by
# an adjacent v0.6.2 pod: the v0.6.1 parser sees a leading ``v2:<hex>:``
# and chokes on the UUID parse of the "v2" literal → ``bad request_id``.
#
# v0.6.2 ships with ``enable_v2_tokens=False`` default — v2 VERIFICATION
# still works (back-compat for anyone who minted v2 before the flag
# existed) but new tokens are minted in v1 format. Flip flips to True in
# v0.6.3 once all rolling-deploy windows are expected to be ≥v0.6.2.
DEFAULT_ENABLE_V2_TOKENS = False

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
    ) -> None:
        from ..audit.module import _check_secret_strength

        _check_secret_strength(secret, "gates secret")
        self._audit = audit
        self._secret = secret
        self._default_expires_in = default_expires_in
        self._store: GatesStore = store or InMemoryGatesStore()
        self._poll_interval_s = poll_interval_s
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
        Once set, ``request()`` and ``wait_for()`` — the agent-driven entry
        points — fail-closed with ``AgentHaltedError`` for a halted agent.

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

    async def wait_for(self, request_id: UUID, timeout: float) -> bool:
        """Block until the request is resolved. Returns True if granted.

        Polls the store every ``poll_interval_s`` seconds. Polling is the
        source of truth — multi-process correct without LISTEN/NOTIFY.

        Raises :class:`ApprovalTimeout` if the timeout elapses without
        resolution. Raises :class:`ApprovalDenied` if the request was denied.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
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
            now = loop.time()
            if now >= deadline:
                raise ApprovalTimeout(
                    f"approval timeout after {timeout}s for request {request_id}"
                )
            # ±20% jitter to avoid thundering-herd polling under load
            jittered = self._poll_interval_s * random.uniform(0.8, 1.2)
            sleep_for = min(jittered, deadline - now)
            await asyncio.sleep(sleep_for)

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
