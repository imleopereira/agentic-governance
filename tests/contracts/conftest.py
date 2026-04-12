"""Shared fixtures for contracts module tests.

The audit_store, secret, and audit fixtures are inherited from tests/conftest.py.
"""
from __future__ import annotations

import secrets as _secrets

import pytest_asyncio

from codeatelier_governance.audit import AuditModule
from codeatelier_governance.contracts import ContractsModule
from codeatelier_governance.cost import CostModule
from codeatelier_governance.gates.module import GatesModule
from codeatelier_governance.gates.store import InMemoryGatesStore
from codeatelier_governance.scope import ScopeModule


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
