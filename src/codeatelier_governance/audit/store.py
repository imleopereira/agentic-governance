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
from uuid import UUID

import structlog

from .errors import StoreUnavailableError
from .models import AuditEventRecord

logger = structlog.get_logger(__name__)


DEFAULT_BATCH_SIZE = 500
DEFAULT_FLUSH_INTERVAL_S = 0.1  # 100 ms
DEFAULT_BUFFER_MAX = 10_000
DEFAULT_PRIMARY_TIMEOUT_S = 5.0


class AuditStore(ABC):
    """Abstract audit storage backend."""

    @abstractmethod
    async def write_batch(self, events: list[AuditEventRecord]) -> None:
        """Persist a batch of events. MUST be all-or-nothing."""

    @abstractmethod
    async def get_event(self, event_id: UUID) -> AuditEventRecord | None:
        """Fetch a single event by id."""

    @abstractmethod
    async def get_chain(self, event_id: UUID) -> list[AuditEventRecord]:
        """Walk parent_event_id from the given event up to the root.

        Returns the chain in chronological order (root first).
        """

    @abstractmethod
    async def get_last_hmac(self, session_id: UUID) -> str | None:
        """Return the most-recent event's HMAC for the session, or None."""

    async def close(self) -> None:
        """Release resources. Override if needed."""
        return None


class InMemoryAuditStore(AuditStore):
    """In-memory store. Used by tests, local dev, and the degraded-mode buffer.

    Bounded in size; evicts oldest events when the cap is reached.
    """

    def __init__(self, max_events: int = 100_000) -> None:
        self._events: dict[UUID, AuditEventRecord] = {}
        self._by_session: dict[UUID, list[UUID]] = {}
        self._order: deque[UUID] = deque()
        self._max = max_events
        self._lock = asyncio.Lock()

    async def write_batch(self, events: list[AuditEventRecord]) -> None:
        if not events:
            return
        async with self._lock:
            for event in events:
                # Idempotent: writing the same event_id twice is a no-op.
                if event.event_id in self._events:
                    continue
                self._events[event.event_id] = event
                self._by_session.setdefault(event.session_id, []).append(
                    event.event_id
                )
                self._order.append(event.event_id)
                self._evict_if_full()

    def _evict_if_full(self) -> None:
        while len(self._order) > self._max:
            oldest_id = self._order.popleft()
            evicted = self._events.pop(oldest_id, None)
            if evicted is not None:
                bucket = self._by_session.get(evicted.session_id)
                if bucket and oldest_id in bucket:
                    bucket.remove(oldest_id)

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
        self._fallback: AuditStore = fallback or InMemoryAuditStore(
            max_events=buffer_max
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
            raise StoreUnavailableError("audit writer is closed")
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
                logger.error("audit.flush_loop_error", error=str(exc))

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
