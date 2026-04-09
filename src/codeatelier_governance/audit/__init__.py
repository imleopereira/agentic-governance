"""Decision audit trail and step-level provenance.

Public API:
    AuditEvent          — input model for logging
    AuditEventRecord    — stored record with chain fields
    EventKind           — canonical event kinds
    AuditModule         — exposed via sdk.audit
    AuditError          — base exception
    ChainIntegrityError — raised when HMAC verification fails
"""
from .errors import (
    AuditError,
    BufferOverflowError,
    ChainIntegrityError,
    StoreUnavailableError,
)
from .models import AuditEvent, AuditEventRecord, EventKind
from .module import AuditModule
from .store import AuditStore, BatchingWriter, InMemoryAuditStore

__all__ = [
    "AuditError",
    "AuditEvent",
    "AuditEventRecord",
    "AuditModule",
    "AuditStore",
    "BatchingWriter",
    "BufferOverflowError",
    "ChainIntegrityError",
    "EventKind",
    "InMemoryAuditStore",
    "StoreUnavailableError",
]
