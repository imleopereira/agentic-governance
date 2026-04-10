"""Shared fixtures for scope module tests."""
from __future__ import annotations

import secrets as _secrets
from typing import AsyncIterator

import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.scope import ScopeModule


@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def audit(
    audit_store: InMemoryAuditStore,
) -> AsyncIterator[AuditModule]:
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(audit_store, secret=_secrets.token_bytes(32), writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()


@pytest_asyncio.fixture
async def scope(audit: AuditModule) -> ScopeModule:
    return ScopeModule(audit)
