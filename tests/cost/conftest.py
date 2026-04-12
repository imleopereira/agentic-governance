"""Shared fixtures for cost module tests.

The audit_store and audit fixtures are inherited from tests/conftest.py.
"""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.audit import AuditModule
from codeatelier_governance.cost import CostModule


@pytest_asyncio.fixture
async def cost(audit: AuditModule) -> CostModule:
    return CostModule(audit)
