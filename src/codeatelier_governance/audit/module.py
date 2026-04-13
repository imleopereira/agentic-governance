"""High-level audit module exposed via ``sdk.audit``.

This is the public surface developers interact with:

    sdk = GovernanceSDK(database_url=..., audit_secret=...)
    await sdk.start()

    # explicit:
    record = await sdk.audit.log(AuditEvent(agent_id="x", kind="tool.call"))

    # decorator (async only):
    @sdk.audit.track(kind="charge_card", agent_id="billing-agent")
    async def charge(amount: int) -> str: ...

    # query the chain:
    chain = await sdk.audit.trace(record.event_id)
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar
from uuid import UUID, uuid4

import structlog

from . import context
from .chain import compute_event_hmac, verify_event
from .errors import ChainIntegrityError
from .models import AuditEvent, AuditEventRecord
from .store import AuditStore, BatchingWriter

logger = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

MAX_PAYLOAD_HASH_BYTES = 65_536  # 64 KiB cap for serialized args/result hashing
MIN_SECRET_BYTES = 32
MIN_SECRET_UNIQUE_BYTES = 8


def _check_secret_strength(secret: bytes, name: str = "audit secret") -> None:
    """Reject obviously weak secrets to prevent placeholder values from
    accidentally reaching production.

    Two checks:
        * Length >= 32 bytes (HMAC-SHA256 key length)
        * At least 8 unique bytes (rules out 'x' * 64 and similar deterministic
          test placeholders)

    Cryptographically these are necessary-but-not-sufficient — a 32-byte
    random secret will trivially pass both — but together they catch the
    common 'I forgot to set the env var' footgun.
    """
    if len(secret) < MIN_SECRET_BYTES:
        raise ValueError(
            f"{name}: must be at least {MIN_SECRET_BYTES} bytes "
            f"(got {len(secret)}). Use secrets.token_bytes(32)."
        )
    if len(set(secret)) < MIN_SECRET_UNIQUE_BYTES:
        raise ValueError(
            f"{name}: rejected weak secret with only {len(set(secret))} "
            f"unique bytes. This usually means a placeholder like "
            f"'x' * 64 leaked into production. Use secrets.token_bytes(32) "
            f"to generate a real key, then store it in your secrets manager."
        )


def _hash_payload(value: Any) -> str:
    """Stable SHA-256 hash of a Python value for input/output_hash fields.

    Falls back to repr() for un-serializable values rather than failing — the
    audit log should never block the host call. Caps input size at 64 KiB to
    prevent pathological inputs from inflating the hash cost.
    """
    if value is None:
        return hashlib.sha256(b"null").hexdigest()
    try:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        serialized = repr(value)
    data = serialized.encode("utf-8")[:MAX_PAYLOAD_HASH_BYTES]
    return hashlib.sha256(data).hexdigest()


class AuditModule:
    """Decision audit trail and step-level provenance.

    Thread-safety: a per-session asyncio.Lock serializes chain construction
    so prev_hash linkage stays monotonic even under concurrent ``log()`` calls
    in the same session. Different sessions never block each other.
    """

    def __init__(
        self,
        store: AuditStore,
        *,
        secret: bytes,
        writer: BatchingWriter | None = None,
        verify_chain_on_read: bool = False,
    ) -> None:
        _check_secret_strength(secret, "audit secret")
        self._store = store
        self._secret = secret
        self._started = False
        self._start_warned = False
        self._verify_chain_on_read = verify_chain_on_read
        # Writer is kept for the BatchingWriter test path and degraded-mode
        # fallback. The chain construction itself goes through the store's
        # insert_with_chain_lock method, which serializes across processes.
        self._writer = writer or BatchingWriter(primary=store)
        self._subscribers: list[
            Callable[[AuditEventRecord], Awaitable[None]]
        ] = []
        # Sessions where the primary store was unreachable on the first
        # attempt. The next successful log in those sessions will emit an
        # additional ``chain.degraded_start`` event so the discontinuity is
        # visible to auditors.
        self._degraded_starts: set[UUID] = set()

    def subscribe(
        self,
        callback: Callable[[AuditEventRecord], Awaitable[None]],
    ) -> None:
        """Register a callback invoked after every successful log.

        Subscribers run AFTER the record is enqueued for write — a failing
        subscriber must never break the audit chain or the host application.
        Exceptions raised by subscribers are caught, logged, and swallowed.

        Used by the OTel exporter and any other consumer that wants to fan
        audit events out to a secondary system (e.g. metrics, dashboards).
        """
        self._subscribers.append(callback)

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self._started = True
        await self._writer.start()

    async def close(self) -> None:
        await self._writer.close()

    # --- context helpers ----------------------------------------------------
    def session(
        self, session_id: UUID | None = None
    ) -> AbstractContextManager[UUID]:
        """Scope a block to an audit session. See :mod:`context`."""
        return context.session(session_id)

    # --- core API -----------------------------------------------------------
    async def log(self, event: AuditEvent) -> AuditEventRecord:
        """Log an audit event. Returns the stored record with chain fields.

        **Non-breaking guarantee:** this method NEVER raises. Audit is an
        observation surface, not an enforcement gate — if our internal
        storage is on fire, the host application call must continue. On
        catastrophic failure (both primary and fallback unreachable, or any
        unexpected internal exception) we log a critical-level message and
        return a placeholder record with ``hmac="0"*64`` and a
        ``metadata["audit.unavailable"] = True`` flag so the caller can
        detect the degraded state if they care to. The host call always
        gets a record back.

        Chain construction is atomic with the insert via the store's
        ``insert_with_chain_lock`` method — under PostgresAuditStore this is
        backed by a per-session ``pg_advisory_xact_lock``, which serializes
        chain operations for the session globally across all worker
        processes connected to the same database. Different sessions never
        block each other.

        On primary store unavailability the call falls through to the
        in-memory fallback writer and emits a ``chain.degraded_start``
        marker on the next successful primary log so auditors can find
        every gap.
        """
        if not self._started and not self._start_warned:
            self._start_warned = True
            logger.warning(
                "audit.sdk_not_started",
                detail=(
                    "GovernanceSDK.start() was not called. "
                    "Use 'async with GovernanceSDK(...) as sdk:' or call "
                    "'await sdk.start()' first. Audit events may not flush."
                ),
            )
        try:
            return await self._log_unsafe(event)
        except Exception as exc:  # noqa: BLE001 - non-breaking guarantee
            logger.critical(
                "audit.log_completely_failed",
                error_type=type(exc).__name__,
                agent_id=event.agent_id,
                kind=event.kind,
            )
            return self._placeholder_record(event)

    async def _maybe_drain_fallback(self) -> None:
        """Best-effort attempt to drain the JSONL fallback into primary.

        Called after a successful recovery (emit_marker path). Failures are
        swallowed — the next successful log will retry.
        """
        fallback = self._writer.fallback
        drain_method = getattr(fallback, "drain_to", None)
        if drain_method is None:
            return
        try:
            count = await drain_method(self._store)
            if count > 0:
                logger.info(
                    "audit.fallback_drained_after_recovery",
                    events=count,
                )
        except Exception as exc:  # noqa: BLE001 - non-breaking
            logger.warning(
                "audit.fallback_drain_attempt_failed",
                error_type=type(exc).__name__,
            )

    def _placeholder_record(self, event: AuditEvent) -> AuditEventRecord:
        """Build a placeholder record returned when storage is unrecoverable.

        The record is NOT in any store. Its hmac is all zeros so verification
        will fail loudly if anyone trusts it. The metadata carries an
        ``audit.unavailable=True`` flag so callers can detect the situation.
        Returning this rather than raising keeps the host application's
        flow alive — the audit substrate has failed but the user's call is
        unaffected.
        """
        session_id = event.session_id or context.current_session() or uuid4()
        parent_id = event.parent_event_id or context.current_parent()
        marker_metadata = dict(event.metadata)
        marker_metadata["audit.unavailable"] = True
        return AuditEventRecord(
            event_id=uuid4(),
            session_id=session_id,
            agent_id=event.agent_id,
            parent_event_id=parent_id,
            kind=event.kind,
            input_hash=event.input_hash,
            output_hash=event.output_hash,
            metadata=marker_metadata,
            prev_hash=None,
            hmac="0" * 64,
            created_at=datetime.now(timezone.utc),
        )

    async def _log_unsafe(self, event: AuditEvent) -> AuditEventRecord:
        """Inner log path that may raise. Wrapped by ``log`` for safety."""
        session_id = event.session_id or context.current_session() or uuid4()
        parent_id = event.parent_event_id or context.current_parent()
        created_at = datetime.now(timezone.utc)

        # If we've previously failed on this session AND the primary is
        # back up, emit a chain.degraded_start marker as the next event so
        # auditors can find the discontinuity.
        emit_marker = session_id in self._degraded_starts

        async def build_record(prev_hash: str | None) -> AuditEventRecord:
            event_id = uuid4()
            mac = compute_event_hmac(
                secret=self._secret,
                event_id=event_id,
                session_id=session_id,
                agent_id=event.agent_id,
                parent_event_id=parent_id,
                kind=event.kind,
                model=event.model,
                input_hash=event.input_hash,
                output_hash=event.output_hash,
                metadata=event.metadata,
                prev_hash=prev_hash,
                created_at=created_at,
            )
            return AuditEventRecord(
                event_id=event_id,
                session_id=session_id,
                agent_id=event.agent_id,
                parent_event_id=parent_id,
                kind=event.kind,
                model=event.model,
                input_hash=event.input_hash,
                output_hash=event.output_hash,
                metadata=event.metadata,
                prev_hash=prev_hash,
                hmac=mac,
                created_at=created_at,
            )

        async def build_marker(prev_hash: str | None) -> AuditEventRecord:
            marker_id = uuid4()
            marker_ts = datetime.now(timezone.utc)
            marker_metadata: dict[str, Any] = {
                "reason": "store unreachable at session start"
            }
            marker_hmac = compute_event_hmac(
                secret=self._secret,
                event_id=marker_id,
                session_id=session_id,
                agent_id=event.agent_id,
                parent_event_id=None,
                kind="chain.degraded_start",
                input_hash=None,
                output_hash=None,
                metadata=marker_metadata,
                prev_hash=prev_hash,
                created_at=marker_ts,
            )
            return AuditEventRecord(
                event_id=marker_id,
                session_id=session_id,
                agent_id=event.agent_id,
                parent_event_id=None,
                kind="chain.degraded_start",
                input_hash=None,
                output_hash=None,
                metadata=marker_metadata,
                prev_hash=prev_hash,
                hmac=marker_hmac,
                created_at=marker_ts,
            )

        try:
            if emit_marker:
                # Primary previously failed. Try emitting a recovery marker
                # on the primary first; the user's event then links to it.
                # Only clear the degraded flag if the marker emit succeeds.
                await self._store.insert_with_chain_lock(session_id, build_marker)
                self._degraded_starts.discard(session_id)
                # Drain any JSONL fallback that's waiting to recover.
                await self._maybe_drain_fallback()
            record = await self._store.insert_with_chain_lock(
                session_id, build_record
            )
        except Exception as exc:
            # Primary store is unreachable. Fall through to the
            # BatchingWriter's in-memory fallback so the host application
            # keeps working. On the FIRST degraded event in a session, also
            # emit a chain.degraded_start marker on the fallback so auditors
            # can find the discontinuity.
            logger.warning(
                "audit.degraded_chain_write",
                session_id=str(session_id),
                error_type=type(exc).__name__,
            )
            is_first_degraded_event = session_id not in self._degraded_starts
            self._degraded_starts.add(session_id)
            fallback_store = self._writer.fallback
            if is_first_degraded_event:
                try:
                    await fallback_store.insert_with_chain_lock(
                        session_id, build_marker
                    )
                except Exception as marker_exc:  # noqa: BLE001 - best effort
                    logger.error(
                        "audit.fallback_marker_failed",
                        session_id=str(session_id),
                        error_type=type(marker_exc).__name__,
                    )
            record = await fallback_store.insert_with_chain_lock(
                session_id, build_record
            )

        # Subscribers run AFTER the record lands in storage. A failing
        # subscriber must never break the audit log itself.
        for sub in self._subscribers:
            try:
                await sub(record)
            except Exception as exc:
                logger.error(
                    "audit.subscriber_error",
                    error_type=type(exc).__name__,
                    event_id=str(record.event_id),
                )
        return record

    async def trace(self, event_id: UUID) -> list[AuditEventRecord]:
        """Return the **provenance** chain from root to ``event_id``.

        Walks ``parent_event_id`` from leaf to root. This is the *causal*
        chain — the events that led up to ``event_id`` via explicit nesting
        (e.g. an LLM call that triggered a tool call that triggered another
        LLM call). It is NOT the same as the HMAC chain (events sharing a
        ``session_id`` linked by ``prev_hash``).

        Use :meth:`trace_session_chain` for the cryptographic chain walk.

        Verifies the HMAC of every record returned. Raises
        :class:`ChainIntegrityError` if any row has been tampered with.
        """
        chain = await self._store.get_chain(event_id)
        for record in chain:
            if not verify_event(record, self._secret):
                raise ChainIntegrityError(
                    f"audit chain integrity violation at event {record.event_id}"
                )
        return chain

    async def trace_session_chain(
        self, session_id: UUID
    ) -> list[AuditEventRecord]:
        """Return every event in a session, in chain (insertion) order.

        Walks the **HMAC chain** for the session — the cryptographically
        tamper-evident sequence of events linked by ``prev_hash``. Each
        record is HMAC-verified; the first failure raises
        :class:`ChainIntegrityError` with the offending event_id.

        This is the API to use when answering "show me everything that
        happened in this session and prove it wasn't tampered with."
        """
        events = await self._store.get_session_events(session_id)
        for record in events:
            if not verify_event(record, self._secret):
                raise ChainIntegrityError(
                    f"audit chain integrity violation at event {record.event_id}"
                )

        # Detect chain forks: two events sharing the same prev_hash means
        # the chain was branched (e.g. an attacker inserted a second root
        # or spliced a parallel branch).
        seen_prev: dict[str | None, UUID] = {}
        for record in events:
            key = record.prev_hash
            if key in seen_prev:
                raise ChainIntegrityError(
                    f"audit chain fork detected: events "
                    f"{seen_prev[key]} and {record.event_id} "
                    f"share prev_hash {key!r}"
                )
            seen_prev[key] = record.event_id

        return events

    # --- public chain verification API --------------------------------------

    async def get_events(self, session_id: UUID) -> list[AuditEventRecord]:
        """Return every event in a session, in chain (insertion) order.

        If ``verify_chain_on_read`` was enabled at construction time, the
        full HMAC chain for the returned window is verified before the
        events are returned. If any link fails, :class:`ChainIntegrityError`
        is raised with the sequence index of the first bad link, and no
        potentially tampered events are returned to the caller.
        """
        events = await self._store.get_session_events(session_id)
        if self._verify_chain_on_read:
            await self.verify_chain_records(events)
        return events

    async def verify_chain_records(
        self,
        records: list[AuditEventRecord],
    ) -> bool:
        """Verify HMAC integrity for an ordered list of AuditEventRecord objects.

        Used internally by ``verify_chain`` and ``get_events`` when
        ``verify_chain_on_read=True``. Raises :class:`ChainIntegrityError`
        at the first failing link. Returns ``True`` if all links are clean.
        """
        for idx, record in enumerate(records):
            if not verify_event(record, self._secret):
                raise ChainIntegrityError(
                    f"audit chain integrity violation at sequence index {idx} "
                    f"(event_id={record.event_id})"
                )
        return True

    async def verify_chain(
        self,
        *,
        from_seq: int | None = None,
        to_seq: int | None = None,
        session_id: UUID | None = None,
    ) -> bool:
        """Verify the HMAC chain for a session or a slice of its events.

        Fetches events in insertion order and verifies each link sequentially.
        If any link fails, raises :class:`ChainIntegrityError` with the
        sequence number (0-based) of the first bad link.

        Parameters
        ----------
        from_seq:
            0-based index of the first event to verify (inclusive).
            ``None`` means start from the beginning.
        to_seq:
            0-based index of the last event to verify (inclusive).
            ``None`` means verify through the end.
        session_id:
            The session whose chain to verify. When ``None`` and both
            ``from_seq`` / ``to_seq`` are ``None``, the entire in-memory
            store is verified event by event (intended for tests and
            in-memory mode only — for large Postgres deployments, pass
            an explicit ``session_id``).

        Returns
        -------
        True
            All checked links are intact.

        Raises
        ------
        ChainIntegrityError
            Raised immediately at the first failing link, carrying the
            0-based sequence number of the bad event.
        """
        if session_id is not None:
            events = await self._store.get_session_events(session_id)
        else:
            # Fall back to the store's full event list for in-memory stores.
            if hasattr(self._store, "_events"):
                from .store import InMemoryAuditStore

                mem: InMemoryAuditStore = self._store  # type: ignore[assignment]
                # Flatten all sessions in insertion order.
                all_ids: list[UUID] = []
                for sid_key in mem._by_session:
                    all_ids.extend(mem._by_session[sid_key])
                events = [mem._events[eid] for eid in all_ids if eid in mem._events]
            else:
                events = []

        # Apply the sequence slice.
        start = from_seq if from_seq is not None else 0
        end = (to_seq + 1) if to_seq is not None else len(events)
        window = events[start:end]

        for idx, record in enumerate(window):
            if not verify_event(record, self._secret):
                absolute_seq = start + idx
                raise ChainIntegrityError(
                    f"audit chain integrity violation at sequence {absolute_seq} "
                    f"(event_id={record.event_id})"
                )
        return True

    # --- decorator ----------------------------------------------------------
    def track(
        self,
        *,
        kind: str,
        agent_id: str | None = None,
        capture_args: bool = True,
        capture_result: bool = True,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """Decorator that logs entry / exit events for an async function.

        On entry, logs ``{kind}.start`` with input_hash. On success, logs
        ``{kind}.end`` with output_hash. On exception, logs ``{kind}.error``
        and re-raises. The wrapped function executes inside a parent_event_id
        context so any nested ``audit.log`` or ``@track`` calls link to it.

        Async-only by design. ``capture_args`` / ``capture_result`` can be
        set False to skip hashing for sensitive functions.
        """

        def decorator(
            func: Callable[P, Awaitable[R]],
        ) -> Callable[P, Awaitable[R]]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"@audit.track requires an async function; "
                    f"{func.__name__} is sync. "
                    f"Fix: convert to `async def {func.__name__}(...)`."
                )

            resolved_agent = agent_id or "unknown"

            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                input_h = (
                    _hash_payload({"args": list(args), "kwargs": dict(kwargs)})
                    if capture_args
                    else None
                )
                start_record = await self.log(
                    AuditEvent(
                        agent_id=resolved_agent,
                        kind=f"{kind}.start",
                        input_hash=input_h,
                    )
                )
                with context.parent_event(start_record.event_id):
                    try:
                        result = await func(*args, **kwargs)
                    except Exception as exc:
                        await self.log(
                            AuditEvent(
                                agent_id=resolved_agent,
                                kind=f"{kind}.error",
                                metadata={"error_type": type(exc).__name__},
                            )
                        )
                        raise
                    output_h = _hash_payload(result) if capture_result else None
                    await self.log(
                        AuditEvent(
                            agent_id=resolved_agent,
                            kind=f"{kind}.end",
                            output_hash=output_h,
                        )
                    )
                    return result

            return wrapper

        return decorator
