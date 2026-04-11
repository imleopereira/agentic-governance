"""Shared fixtures for contracts module tests."""
from __future__ import annotations

import secrets as _secrets
from typing import AsyncIterator

import pytest_asyncio

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.contracts import ContractsModule
from codeatelier_governance.cost import CostModule
from codeatelier_governance.gates.module import GatesModule
from codeatelier_governance.gates.store import InMemoryGatesStore
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
    secret = _secrets.token_bytes(32)
    module = AuditModule(audit_store, secret=secret, writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()


@pytest_asyncio.fixture
async def scope(audit: AuditModule) -> ScopeModule:
    return ScopeModule(audit)


@pytest_asyncio.fixture
async def cost(audit: AuditModule) -> CostModule:
    return CostModule(audit)


@pytest_asyncio.fixture
async def gates(audit: AuditModule) -> GatesModule:
    secret = _secrets.token_bytes(32)
    store = InMemoryGatesStore()
    return GatesModule(audit, secret=secret, store=store)


@pytest_asyncio.fixture
async def contracts(
    audit: AuditModule,
    scope: ScopeModule,
    cost: CostModule,
    gates: GatesModule,
) -> ContractsModule:
    return ContractsModule(audit, scope, cost, gates=gates)
