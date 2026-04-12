"""On-disk JSONL fallback store for audit events.

When the primary store (Postgres) is unreachable, audit events fall through
to this store rather than being lost. The events are written to a JSONL
file on the local filesystem so they survive process restarts and crashes.

On the next successful primary write, the SDK can call ``drain_to(primary)``
to replay the buffered events into the real database. After a successful
drain the JSONL file is truncated.

Trade-offs vs in-memory only:
    + Durable across process restarts and crashes
    + Operators can inspect / forensic the file directly
    + No data loss as long as the host's local disk survives
    - Adds local filesystem as a dependency (still no new infrastructure)
    - Slow under high write rate compared to in-memory
    - Per-line append uses an asyncio.Lock; not lock-free
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from .models import AuditEventRecord
from .store import AuditStore, ChainBuilder

logger = structlog.get_logger(__name__)


class JsonlFallbackStore(AuditStore):
    """Append-only JSONL file backing the audit fallback path.

    Each line is a JSON-encoded :class:`AuditEventRecord`. Reads scan the
    file (slow but rare — only used during chain hydration on a degraded
    session). Writes append (fast). The chain construction logic mirrors
    InMemoryAuditStore: a per-session asyncio.Lock serializes inserts so
    prev_hash linkage is monotonic within each session.
    """

    # Maximum fallback file size before rotation (default 50 MB).
    _MAX_FILE_BYTES: int = 50 * 1024 * 1024
    # Batch size for chunked drain operations.
    _DRAIN_BATCH_SIZE: int = 1000

    def __init__(
        self,
        path: str | Path,
        *,
        max_file_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._path = Path(path)
        self._MAX_FILE_BYTES = max_file_bytes
        self._fs_available = True
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Harden file permissions: owner-only read/write (0600)
            if self._path.exists():
                self._path.chmod(0o600)
        except OSError as exc:
            logger.warning(
                "audit.jsonl_fallback_fs_unavailable",
                path=str(self._path),
                error_type=type(exc).__name__,
                detail="Falling back to in-memory buffer. Events will not survive restarts.",
            )
            self._fs_available = False
        self._memory_buffer: list[AuditEventRecord] = []
        self._global_lock = asyncio.Lock()
        self._session_locks: dict[UUID, asyncio.Lock] = {}
        # Cache the most-recent hmac per session in memory so we don't
        # rescan the file on every event. Hydrated lazily from disk on the
        # first event in each session.
        self._last_hmac: dict[UUID, str | None] = {}
        self._initialized: set[UUID] = set()

    async def insert_with_chain_lock(
        self,
        session_id: UUID,
        builder: ChainBuilder,
    ) -> AuditEventRecord:
        sess_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with sess_lock:
            if session_id not in self._initialized:
                self._last_hmac[session_id] = (
                    await self._scan_last_hmac(session_id)
                )
                self._initialized.add(session_id)
            prev_hash = self._last_hmac.get(session_id)
            record = await builder(prev_hash)
            await self._append(record)
            self._last_hmac[session_id] = record.hmac
        return record

    async def write_batch(self, events: list[AuditEventRecord]) -> None:
        """Write a batch as-is, preserving each row's existing chain fields.

        Used by the drain path: events are already chain-linked from the
        original session and we just want to flush them to disk in order.
        """
        async with self._global_lock:
            if not self._fs_available:
                self._memory_buffer.extend(events)
                return
            try:
                created = not self._path.exists()
                with self._path.open("a") as fp:
                    for event in events:
                        fp.write(_serialize(event) + "\n")
                if created:
                    self._path.chmod(0o600)
            except OSError as exc:
                logger.warning(
                    "audit.jsonl_write_batch_fs_failed",
                    error_type=type(exc).__name__,
                )
                self._fs_available = False
                self._memory_buffer.extend(events)

    async def get_event(self, event_id: UUID) -> AuditEventRecord | None:
        async with self._global_lock:
            for event in self._read_all():
                if event.event_id == event_id:
                    return event
        return None

    async def get_chain(self, event_id: UUID) -> list[AuditEventRecord]:
        # Walk parent_event_id from leaf back to root
        chain: list[AuditEventRecord] = []
        current: UUID | None = event_id
        visited: set[UUID] = set()
        async with self._global_lock:
            events_by_id = {e.event_id: e for e in self._read_all()}
        while current is not None:
            if current in visited:
                break
            visited.add(current)
            event = events_by_id.get(current)
            if event is None:
                break
            chain.append(event)
            current = event.parent_event_id
        return list(reversed(chain))

    async def get_last_hmac(self, session_id: UUID) -> str | None:
        return await self._scan_last_hmac(session_id)

    async def count(self) -> int:
        async with self._global_lock:
            if not self._path.exists():
                return 0
            with self._path.open() as fp:
                return sum(1 for line in fp if line.strip())

    async def drain_to(self, target: AuditStore) -> int:
        """Drain events from the JSONL into ``target`` in batches.

        Reads and writes in chunks of ``_DRAIN_BATCH_SIZE`` (default 1000)
        to avoid loading the entire file into memory at once. On success,
        truncates the file. On failure mid-drain, leaves the remaining
        events intact so the operator can retry.

        Also drains any in-memory buffer (from read-only FS fallback).

        Returns the total number of events drained.
        """
        total_drained = 0

        async with self._global_lock:
            # Drain in-memory buffer first (from read-only FS fallback)
            if self._memory_buffer:
                mem_events = list(self._memory_buffer)
                for i in range(0, len(mem_events), self._DRAIN_BATCH_SIZE):
                    batch = mem_events[i : i + self._DRAIN_BATCH_SIZE]
                    try:
                        await target.write_batch(batch)
                        total_drained += len(batch)
                    except Exception as exc:
                        logger.error(
                            "audit.jsonl_drain_memory_failed",
                            error_type=type(exc).__name__,
                            pending=len(mem_events) - i,
                        )
                        # Keep undrained events in memory buffer
                        self._memory_buffer = mem_events[i:]
                        if total_drained > 0:
                            logger.info("audit.jsonl_drained", events=total_drained)
                        raise
                self._memory_buffer.clear()

            # Drain disk file in chunks
            if not self._fs_available or not self._path.exists():
                if total_drained > 0:
                    logger.info("audit.jsonl_drained", events=total_drained)
                return total_drained

            # Stream from file in batches to avoid loading everything
            file_batch: list[AuditEventRecord] = []
            try:
                with self._path.open() as fp:
                    for line in fp:
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        try:
                            file_batch.append(_deserialize(data))
                        except Exception:
                            continue
                        if len(file_batch) >= self._DRAIN_BATCH_SIZE:
                            await target.write_batch(file_batch)
                            total_drained += len(file_batch)
                            file_batch = []

                # Flush remaining batch
                if file_batch:
                    await target.write_batch(file_batch)
                    total_drained += len(file_batch)
            except Exception as exc:
                logger.error(
                    "audit.jsonl_drain_failed",
                    error_type=type(exc).__name__,
                    drained_so_far=total_drained,
                )
                raise

            # Drain succeeded — truncate the file
            self._path.unlink()
            self._initialized.clear()
            self._last_hmac.clear()

        if total_drained > 0:
            logger.info("audit.jsonl_drained", events=total_drained)
        return total_drained

    async def close(self) -> None:
        return None

    # --- internals ---------------------------------------------------------
    async def _append(self, record: AuditEventRecord) -> None:
        async with self._global_lock:
            if not self._fs_available:
                self._memory_buffer.append(record)
                return
            try:
                # Rotate if file exceeds size cap
                if self._path.exists():
                    try:
                        size = self._path.stat().st_size
                    except OSError:
                        size = 0
                    if size >= self._MAX_FILE_BYTES:
                        rotated = self._path.with_suffix(".jsonl.1")
                        try:
                            self._path.rename(rotated)
                            logger.warning(
                                "audit.jsonl_fallback_rotated",
                                old_size_bytes=size,
                                rotated_path=str(rotated),
                            )
                        except OSError as exc:
                            logger.warning(
                                "audit.jsonl_fallback_rotate_failed",
                                error_type=type(exc).__name__,
                            )
                            # Truncate instead of rotating
                            self._path.unlink(missing_ok=True)

                created = not self._path.exists()
                with self._path.open("a") as fp:
                    fp.write(_serialize(record) + "\n")
                if created:
                    self._path.chmod(0o600)
            except OSError as exc:
                logger.warning(
                    "audit.jsonl_append_fs_failed",
                    error_type=type(exc).__name__,
                    detail="Falling back to in-memory buffer.",
                )
                self._fs_available = False
                self._memory_buffer.append(record)

    async def _scan_last_hmac(self, session_id: UUID) -> str | None:
        """Scan the file for the most recent event in the session.

        O(N) on file size. Only called once per session (on first event)
        thanks to the in-memory ``_last_hmac`` cache. Also checks the
        in-memory buffer when the filesystem is unavailable.
        """
        async with self._global_lock:
            last: str | None = None
            # Scan disk file if available
            if self._fs_available and self._path.exists():
                with self._path.open() as fp:
                    for line in fp:
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if str(data.get("session_id")) == str(session_id):
                            last = data.get("hmac")
            # Also scan in-memory buffer
            for record in self._memory_buffer:
                if record.session_id == session_id:
                    last = record.hmac
            return last

    def _read_all(self) -> list[AuditEventRecord]:
        if not self._path.exists():
            return []
        out: list[AuditEventRecord] = []
        with self._path.open() as fp:
            for line in fp:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    out.append(_deserialize(data))
                except Exception:
                    continue
        return out


def _serialize(record: AuditEventRecord) -> str:
    payload: dict[str, Any] = {
        "event_id": str(record.event_id),
        "session_id": str(record.session_id),
        "agent_id": record.agent_id,
        "parent_event_id": (
            str(record.parent_event_id)
            if record.parent_event_id is not None
            else None
        ),
        "kind": record.kind,
        "input_hash": record.input_hash,
        "output_hash": record.output_hash,
        "metadata": record.metadata,
        "prev_hash": record.prev_hash,
        "hmac": record.hmac,
        "created_at": record.created_at.isoformat(),
    }
    return json.dumps(payload, separators=(",", ":"))


def _deserialize(data: dict[str, Any]) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=UUID(data["event_id"]),
        session_id=UUID(data["session_id"]),
        agent_id=data["agent_id"],
        parent_event_id=(
            UUID(data["parent_event_id"])
            if data.get("parent_event_id")
            else None
        ),
        kind=data["kind"],
        input_hash=data.get("input_hash"),
        output_hash=data.get("output_hash"),
        metadata=data.get("metadata") or {},
        prev_hash=data.get("prev_hash"),
        hmac=data["hmac"],
        created_at=datetime.fromisoformat(data["created_at"]),
    )
