"""Shared utilities for the Code Atelier Governance SDK.

This module provides:
    normalize_db_url   -- Convert postgresql:// to async driver URL (one copy, not four)
    sanitize_db_error  -- Strip connection strings and SQL from exception messages
"""
from __future__ import annotations

import re


def normalize_db_url(url: str, *, component: str = "store") -> str:
    """Convert plain ``postgresql://`` URLs to the async asyncpg dialect.

    Args:
        url: A PostgreSQL connection string.
        component: Name of the calling component (for error messages).

    Raises:
        ValueError: If the URL does not start with ``postgresql://``.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    raise ValueError(
        f"{component}: expected a postgresql:// connection string.\n"
        f"Fix: pass database_url=os.environ['GOVERNANCE_DATABASE_URL']"
    )


# Pattern that matches postgresql://user:pass@host/db style URLs anywhere
# in a string. Intentionally broad to catch all variants.
_DB_URL_PATTERN = re.compile(
    r"postgresql(?:\+\w+)?://[^\s,;'\")]*",
    re.IGNORECASE,
)

# Pattern matching SQL fragments that commonly leak through exceptions
_SQL_PATTERN = re.compile(
    r"(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|FROM|WHERE|INTO)\b",
    re.IGNORECASE,
)


def sanitize_db_error(exc: BaseException) -> str:
    """Return a safe error summary that never leaks DB URLs or SQL.

    Used in every code path where a database exception might propagate to
    an HTTP caller, CLI output, or structured log value.

    Returns only ``type(exc).__name__`` if the message contains sensitive
    content, otherwise returns the type name plus a scrubbed message.
    """
    exc_type = type(exc).__name__
    raw = str(exc)
    if _DB_URL_PATTERN.search(raw) or _SQL_PATTERN.search(raw):
        return exc_type
    # Truncate long messages to avoid log bloat
    if len(raw) > 200:
        raw = raw[:200] + "..."
    return f"{exc_type}: {raw}"
