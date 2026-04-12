"""Shared fixtures for audit tests.

The audit_store and audit fixtures are inherited from tests/conftest.py.
This file provides the 'store' alias used by audit-specific tests.
"""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.audit import InMemoryAuditStore


@pytest_asyncio.fixture
async def store(audit_store: InMemoryAuditStore) -> InMemoryAuditStore:
    """Alias: audit tests historically use 'store' instead of 'audit_store'."""
    return audit_store
