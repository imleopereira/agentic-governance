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

import asyncio
import functools
import hashlib
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
    ) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise ValueError(
                f"audit secret must be at least {MIN_SECRET_BYTES} bytes; "
                f"use secrets.token_bytes(32) or set GOVERNANCE_AUDIT_SECRET."
            )
        self._store = store
        self._secret = secret
        self._writer = writer or BatchingWriter(primary=store)
        self._session_locks: dict[UUID, asyncio.Lock] = {}
        self._last_hmac: dict[UUID, str | None] = {}
        self._initialized_sessions: set[UUID] = set()

    # --- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
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

        The event is enqueued for async write; this call returns once the
        chain fields (event_id, prev_hash, hmac, created_at) are computed and
        the row is in the in-flight buffer. Actual DB persistence happens in
        the background flush loop.
        """
        session_id = event.session_id or context.current_session() or uuid4()
        parent_id = event.parent_event_id or context.current_parent()
        event_id = uuid4()
        created_at = datetime.now(timezone.utc)

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if session_id not in self._initialized_sessions:
                # On first log() in a session, hydrate from the store so we
                # continue any chain that already exists in the DB.
                self._last_hmac[session_id] = await self._store.get_last_hmac(
                    session_id
                )
                self._initialized_sessions.add(session_id)
            prev_hash = self._last_hmac.get(session_id)

            mac = compute_event_hmac(
                secret=self._secret,
                event_id=event_id,
                session_id=session_id,
                agent_id=event.agent_id,
                parent_event_id=parent_id,
                kind=event.kind,
                input_hash=event.input_hash,
                output_hash=event.output_hash,
                metadata=event.metadata,
                prev_hash=prev_hash,
                created_at=created_at,
            )
            record = AuditEventRecord(
                event_id=event_id,
                session_id=session_id,
                agent_id=event.agent_id,
                parent_event_id=parent_id,
                kind=event.kind,
                input_hash=event.input_hash,
                output_hash=event.output_hash,
                metadata=event.metadata,
                prev_hash=prev_hash,
                hmac=mac,
                created_at=created_at,
            )
            self._last_hmac[session_id] = mac
            await self._writer.enqueue(record)
        return record

    async def trace(self, event_id: UUID) -> list[AuditEventRecord]:
        """Return the provenance chain from root to ``event_id``.

        Verifies the HMAC of every record in the chain. Raises
        :class:`ChainIntegrityError` if any row has been tampered with.
        """
        chain = await self._store.get_chain(event_id)
        for record in chain:
            if not verify_event(record, self._secret):
                raise ChainIntegrityError(
                    f"audit chain integrity violation at event {record.event_id}"
                )
        return chain

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
            if not asyncio.iscoroutinefunction(func):
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
