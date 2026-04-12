"""Shared fixtures for scope module tests.

The audit_store and audit fixtures are inherited from tests/conftest.py.
"""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.audit import AuditModule
from codeatelier_governance.scope import ScopeModule


@pytest_asyncio.fixture
async def scope(audit: AuditModule) -> ScopeModule:
    return ScopeModule(audit)
