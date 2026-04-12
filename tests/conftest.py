"""Root-level shared fixtures for governance SDK tests.

Centralizes the audit_store + audit fixture pair that was previously
copy-pasted across 6+ module-level conftest files.
"""
from __future__ import annotations

import secrets as _secrets
from typing import AsyncIterator

import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)


@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    """Shared in-memory audit store."""
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def secret() -> bytes:
    """Random 32-byte HMAC secret for tests."""
    return _secrets.token_bytes(32)


@pytest_asyncio.fixture
async def audit(
    audit_store: InMemoryAuditStore, secret: bytes
) -> AsyncIterator[AuditModule]:
    """Fully wired AuditModule with batching writer, started and torn down."""
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(audit_store, secret=secret, writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()
