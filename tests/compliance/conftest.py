"""Shared fixtures for compliance report tests.

The audit_store and audit fixtures are inherited from tests/conftest.py.
"""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.audit import InMemoryAuditStore
from codeatelier_governance.compliance.report import ReportGenerator


@pytest_asyncio.fixture
async def generator(audit_store: InMemoryAuditStore) -> ReportGenerator:
    return ReportGenerator(audit_store=audit_store)
