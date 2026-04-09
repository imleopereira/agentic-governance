"""Audit module exceptions.

Errors raised to callers must never include secrets, DB URLs, or internal paths.
"""
from __future__ import annotations


class AuditError(Exception):
    """Base class for all audit module errors."""


class ChainIntegrityError(AuditError):
    """Raised when HMAC verification of an audit row fails (tamper detected)."""


class StoreUnavailableError(AuditError):
    """Raised when the audit store cannot accept writes (DB down, etc)."""


class BufferOverflowError(AuditError):
    """Raised when the in-memory degraded-mode buffer cannot accept more events."""
