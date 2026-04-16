"""Decision audit trail and step-level provenance.

Public API:
    AuditEvent          — input model for logging
    AuditEventRecord    — stored record with chain fields
    EventKind           — canonical event kinds
    AuditModule         — exposed via sdk.audit
    AuditError          — base exception
    ChainIntegrityError — raised when HMAC verification fails
"""
from .corrections import (
    ALLOWED_REASONS,
    NOTE_MAX_CHARS,
    CorrectionValidationError,
    validate_correction_payload,
)
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
    "ALLOWED_REASONS",
    "AuditError",
    "AuditEvent",
    "AuditEventRecord",
    "AuditModule",
    "AuditStore",
    "BatchingWriter",
    "BufferOverflowError",
    "ChainIntegrityError",
    "CorrectionValidationError",
    "EventKind",
    "InMemoryAuditStore",
    "NOTE_MAX_CHARS",
    "StoreUnavailableError",
    "validate_correction_payload",
]
