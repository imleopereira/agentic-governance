"""Audit store abstraction + in-memory implementation + batching writer.

Three pieces:
    AuditStore         — abstract interface; backends implement this
    InMemoryAuditStore — used for tests, local dev, and as the degraded-mode buffer
    BatchingWriter     — flushes to a primary store every 100ms or 500 events,
                         spills to a fallback (in-memory by default) when the
                         primary errors. Never silently drops.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID

import structlog

from .errors import ChainIntegrityError, StoreUnavailableError
from .models import AuditEventRecord

ChainBuilder = Callable[[str | None], Awaitable[AuditEventRecord]]

logger = structlog.get_logger(__name__)


DEFAULT_BATCH_SIZE = 500
DEFAULT_FLUSH_INTERVAL_S = 0.1  # 100 ms
DEFAULT_BUFFER_MAX = 10_000
DEFAULT_PRIMARY_TIMEOUT_S = 5.0


class AuditStore(ABC):
    """Abstract audit storage backend."""

    @abstractmethod
    async def insert_with_chain_lock(
        self,
        session_id: UUID,
        builder: ChainBuilder,
    ) -> AuditEventRecord:
        """Atomically: lock the session, read the latest hmac, build a new
        record using the prev_hash, insert it, release the lock.

        The ``builder`` callback receives the latest hmac (or None if no
        events exist for the session yet) and returns the new
        :class:`AuditEventRecord` to insert. The store guarantees no other
        process can interleave a chain operation for the same session_id
        between the read and the insert.

        Implementations:
            - InMemoryAuditStore uses a per-session asyncio.Lock
            - PostgresAuditStore uses pg_advisory_xact_lock over a 64-bit key
              derived from the first 8 bytes of the session UUID (see
              _session_lock_key) — deliberately NOT hashtext(session_id),
              which is only 32-bit and would collide via the birthday bound

        Different sessions never block each other.

        Raises:
            StoreUnavailableError: the underlying store is unreachable.
        """

    @abstractmethod
    async def write_batch(self, events: list[AuditEventRecord]) -> None:
        """Persist a batch of events. MUST be all-or-nothing.

        Used by ``BatchingWriter`` for non-chain writes (degraded-mode
        spillover etc.). The chain path uses ``insert_with_chain_lock``.
        """

    @abstractmethod
    async def get_event(self, event_id: UUID) -> AuditEventRecord | None:
        """Fetch a single event by id."""

    @abstractmethod
    async def get_chain(self, event_id: UUID) -> list[AuditEventRecord]:
        """Walk parent_event_id from the given event up to the root.

        Returns the chain in chronological order (root first).
        """

    async def get_session_events(
        self, session_id: UUID
    ) -> list[AuditEventRecord]:
        """Return every event in a session, in chain insertion order.

        Used by ``audit.trace_session_chain`` to walk the HMAC chain
        (distinct from the parent_event_id provenance walk in get_chain).
        Default implementation falls back to scanning everything; backends
        with native session indexes should override.
        """
        return []

    @abstractmethod
    async def get_last_hmac(self, session_id: UUID) -> str | None:
        """Return the most-recent event's HMAC for the session, or None.

        Kept for backwards compatibility with the BatchingWriter path. New
        code should use ``insert_with_chain_lock`` for chain construction.
        """

    async def close(self) -> None:
        """Release resources. Override if needed."""
        return None


class InMemoryAuditStore(AuditStore):
    """In-memory store. Used by tests, local dev, and the degraded-mode buffer.

    Bounded in size; behavior at the cap is controlled by ``on_full``.

    v0.6.2 Bug #11 hardening: prior to v0.6.2 this store silently evicted
    the oldest event on overflow, which meant tests running against the
    in-memory store gave false-green on any code path that would break in
    Postgres (which enforces append-only via triggers). A session with
    > ``max_events`` events had its HMAC chain silently truncated.

    Default behavior is now ``on_full="raise"`` — tests and production
    code see the bug immediately. ``on_full="evict"`` preserves ring-
    buffer semantics for callers who legitimately want a bounded buffer
    (e.g. the degraded-mode fallback under ``BatchingWriter``); on every
    eviction a structlog WARN is emitted AND a counter
    (``stats()["evicted_total"]``) is incremented so the truncation is
    never silent.
    """

    def __init__(
        self,
        max_events: int = 100_000,
        *,
        on_full: Literal["evict", "raise"] = "raise",
    ) -> None:
        self._events: dict[UUID, AuditEventRecord] = {}
        self._by_session: dict[UUID, list[UUID]] = {}
        self._order: deque[UUID] = deque()
        self._max = max_events
        self._on_full = on_full
        self._evicted_total = 0
        self._lock = asyncio.Lock()
        self._session_locks: dict[UUID, asyncio.Lock] = {}

    async def insert_with_chain_lock(
        self,
        session_id: UUID,
        builder: ChainBuilder,
    ) -> AuditEventRecord:
        # Per-session lock — different sessions never block each other.
        sess_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with sess_lock:
            ids = self._by_session.get(session_id, [])
            prev_hash = self._events[ids[-1]].hmac if ids else None
            record = await builder(prev_hash)
            async with self._lock:
                # v0.6.2 Bug #11: check the cap BEFORE landing the
                # record so on_full='raise' refuses the write cleanly
                # without leaving a partially-appended row that would
                # later trip verify_chain at a bogus index.
                self._enforce_cap_before_append(record.event_id)
                self._events[record.event_id] = record
                self._by_session.setdefault(session_id, []).append(record.event_id)
                self._order.append(record.event_id)
        return record

    async def write_batch(self, events: list[AuditEventRecord]) -> None:
        if not events:
            return
        async with self._lock:
            for event in events:
                # Idempotent: writing the same event_id twice is a no-op.
                if event.event_id in self._events:
                    continue
                # v0.6.2 Bug #11: same pre-append gate as the chain
                # path. If the cap is exceeded under on_full='raise' we
                # refuse the entire remaining batch (the caller's
                # ``all-or-nothing`` contract), leaving prior loop
                # iterations visible — documented by the raise semantic.
                self._enforce_cap_before_append(event.event_id)
                self._events[event.event_id] = event
                self._by_session.setdefault(event.session_id, []).append(
                    event.event_id
                )
                self._order.append(event.event_id)

    def _enforce_cap_before_append(self, incoming_event_id: UUID) -> None:
        """Check the cap BEFORE the incoming write lands.

        Under on_full='raise' this refuses the write cleanly with no
        partial-state side effects. Under on_full='evict' this pops the
        oldest event(s) to make room so the incoming write fits without
        exceeding the cap.
        """
        if len(self._order) < self._max:
            return
        if self._on_full == "raise":
            raise StoreUnavailableError(
                "InMemoryAuditStore: max_events cap reached "
                f"({self._max}). Default on_full='raise' refuses to "
                "silently evict — a silently-truncated HMAC chain would "
                "make tests false-green on code paths that fail in "
                "Postgres (which enforces append-only).\n"
                "Fix: either raise max_events, or explicitly opt in to "
                "ring-buffer semantics with "
                "InMemoryAuditStore(max_events=N, on_full='evict').",
                recovery_hint=(
                    "Increase max_events or pass on_full='evict' if you "
                    "really want a bounded ring buffer."
                ),
            )
        # Eviction path: pop enough oldest events to leave room for the
        # incoming write. In practice the loop body runs once, but the
        # ``while`` shape tolerates out-of-band state (e.g. a mutated
        # _max between constructions).
        while len(self._order) >= self._max:
            oldest_id = self._order.popleft()
            evicted = self._events.pop(oldest_id, None)
            if evicted is not None:
                bucket = self._by_session.get(evicted.session_id)
                if bucket and oldest_id in bucket:
                    bucket.remove(oldest_id)
            self._evicted_total += 1
            logger.warning(
                "audit.memory_store_evicted",
                oldest_event_id=str(oldest_id),
                evicted_count=self._evicted_total,
                max_events=self._max,
                incoming_event_id=str(incoming_event_id),
                detail=(
                    "InMemoryAuditStore evicted the oldest event to "
                    "keep size <= max_events. The HMAC chain on this "
                    "store is now truncated — verify_chain / "
                    "verify_not_truncated will report the gap."
                ),
            )

    def stats(self) -> dict[str, Any]:
        """Observability hook for the v0.6.2 Bug #11 eviction counter.

        Returns a dict with at least:
          * ``evicted_total`` — number of events dropped by the ring
            buffer since the store was constructed. ``> 0`` means the
            HMAC chain has been truncated and any verify_chain pass
            against this store is unsound.
          * ``max_events`` — configured cap.
          * ``on_full`` — ``"evict"`` or ``"raise"``.
          * ``size`` — current event count.
        """
        return {
            "evicted_total": self._evicted_total,
            "max_events": self._max,
            "on_full": self._on_full,
            "size": len(self._events),
        }

    def verify_not_truncated(self) -> bool:
        """Chain-verifier hook: raise if the store has been truncated.

        The existing ``AuditModule.verify_chain`` only inspects HMAC
        linkage between CONSECUTIVE events returned by
        ``get_session_events`` — it cannot see that earlier events were
        dropped by a ring-buffer eviction. v0.6.2 Bug #11 closes that
        gap by making truncation an explicit failure signal on the
        store itself. Callers who want a full chain proof should call
        both ``AuditModule.verify_chain`` (HMAC + linkage) AND
        ``InMemoryAuditStore.verify_not_truncated`` (no silent drops).
        """
        if self._evicted_total > 0:
            raise ChainIntegrityError(
                "InMemoryAuditStore chain truncated: "
                f"{self._evicted_total} event(s) evicted by the ring "
                "buffer. verify_chain cannot reconstruct the dropped "
                "prefix — the earliest remaining event has no "
                "verifiable prev_hash linkage to the original root."
            )
        return True

    async def get_event(self, event_id: UUID) -> AuditEventRecord | None:
        return self._events.get(event_id)

    async def get_chain(self, event_id: UUID) -> list[AuditEventRecord]:
        chain: list[AuditEventRecord] = []
        current: UUID | None = event_id
        visited: set[UUID] = set()
        while current is not None:
            if current in visited:  # cycle guard (defensive; should never happen)
                break
            visited.add(current)
            event = self._events.get(current)
            if event is None:
                break
            chain.append(event)
            current = event.parent_event_id
        return list(reversed(chain))

    async def get_last_hmac(self, session_id: UUID) -> str | None:
        ids = self._by_session.get(session_id)
        if not ids:
            return None
        return self._events[ids[-1]].hmac

    async def get_session_events(
        self, session_id: UUID
    ) -> list[AuditEventRecord]:
        ids = self._by_session.get(session_id, [])
        return [self._events[i] for i in ids if i in self._events]

    async def count(self) -> int:
        return len(self._events)


class BatchingWriter:
    """Async write buffer with size+time batching and degraded-mode fallback.

    Behavior:
        * Events queued via ``enqueue()`` flush to ``primary`` when EITHER
          ``batch_size`` events accumulate OR ``flush_interval_s`` elapses.
        * If the primary write fails, the batch spills to ``fallback``
          (default: an InMemoryAuditStore) and the writer enters degraded
          mode. The next successful flush returns it to normal.
        * If the in-memory buffer fills past ``buffer_max`` while the primary
          is degraded, new events go straight to the fallback. Nothing is
          ever silently dropped.
    """

    def __init__(
        self,
        primary: AuditStore,
        *,
        fallback: AuditStore | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        buffer_max: int = DEFAULT_BUFFER_MAX,
        primary_timeout_s: float = DEFAULT_PRIMARY_TIMEOUT_S,
    ) -> None:
        self._primary = primary
        # Degraded-mode fallback: explicitly opt in to ring-buffer
        # semantics. The BatchingWriter's job is to never silently drop,
        # and if the fallback's cap is exceeded it handles spillover
        # upstream — so eviction here is a tolerated last-resort signal,
        # not a false-green. The per-eviction WARN from the store still
        # fires so operators see the truncation.
        self._fallback: AuditStore = fallback or InMemoryAuditStore(
            max_events=buffer_max, on_full="evict"
        )
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._buffer_max = buffer_max
        self._primary_timeout_s = primary_timeout_s
        self._buffer: deque[AuditEventRecord] = deque()
        self._lock = asyncio.Lock()
        self._flush_event = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task[None] | None = None
        self._degraded = False

    @property
    def primary(self) -> AuditStore:
        return self._primary

    @property
    def fallback(self) -> AuditStore:
        return self._fallback

    @property
    def degraded(self) -> bool:
        return self._degraded

    async def start(self) -> None:
        """Start the background flush loop. Idempotent."""
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="codeatelier-audit-flush"
            )

    async def enqueue(self, event: AuditEventRecord) -> None:
        """Queue an event for asynchronous write.

        If the buffer is full (degraded mode + slow drain), the event is
        written directly to the fallback store rather than dropped.
        """
        if self._closed:
            raise StoreUnavailableError(
                "audit.enqueue failed: the BatchingWriter has already been closed.\n"
                "Expected: enqueue() called before close() or within an `async with sdk:` block.\n"
                "Fix: ensure sdk.close() is only called once, after all events are logged.",
                recovery_hint="Ensure sdk.close() or sdk.__aexit__ is only called once, after all events are logged.",
            )
        async with self._lock:
            if len(self._buffer) >= self._buffer_max:
                logger.warning(
                    "audit.buffer_overflow_spill",
                    buffer_len=len(self._buffer),
                    event_id=str(event.event_id),
                )
                # Spill to fallback rather than drop.
                await self._fallback.write_batch([event])
                return
            self._buffer.append(event)
            if len(self._buffer) >= self._batch_size:
                self._flush_event.set()

    async def flush(self) -> None:
        """Force a flush. Useful for tests and graceful shutdown."""
        await self._flush_once()

    async def _run(self) -> None:
        while not self._closed:
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(),
                    timeout=self._flush_interval_s,
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            try:
                await self._flush_once()
            except Exception as exc:  # never let the loop die
                logger.error(
                    "audit.flush_loop_error",
                    error_type=type(exc).__name__,
                )

    async def _flush_once(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch = list(self._buffer)
            self._buffer.clear()
        try:
            await asyncio.wait_for(
                self._primary.write_batch(batch),
                timeout=self._primary_timeout_s,
            )
            if self._degraded:
                logger.info(
                    "audit.primary_recovered", events_flushed=len(batch)
                )
                self._degraded = False
        except Exception as exc:
            self._degraded = True
            logger.error(
                "audit.primary_write_failed_degraded",
                error_type=type(exc).__name__,
                events_buffered=len(batch),
            )
            try:
                await self._fallback.write_batch(batch)
            except Exception as fb_exc:
                logger.critical(
                    "audit.fallback_write_failed",
                    error_type=type(fb_exc).__name__,
                    events_at_risk=len(batch),
                )
                # Last-resort: re-queue. Bounded by buffer_max so we never
                # explode memory; oldest events get dropped to fallback in
                # the next enqueue cycle.
                async with self._lock:
                    space = self._buffer_max - len(self._buffer)
                    for event in batch[:space]:
                        self._buffer.append(event)

    async def close(self) -> None:
        """Stop the flush loop, drain the buffer, dispose of stores."""
        self._closed = True
        self._flush_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        await self._flush_once()  # final drain
        await self._primary.close()
        await self._fallback.close()
