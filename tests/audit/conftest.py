"""Shared fixtures for audit tests."""
from __future__ import annotations

import secrets
from typing import AsyncIterator

import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)


@pytest_asyncio.fixture
async def store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def secret() -> bytes:
    return secrets.token_bytes(32)


@pytest_asyncio.fixture
async def audit(
    store: InMemoryAuditStore, secret: bytes
) -> AsyncIterator[AuditModule]:
    writer = BatchingWriter(
        primary=store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(store, secret=secret, writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()
