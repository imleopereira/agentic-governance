"""Shared fixtures for loop module tests.

The audit_store and audit fixtures are inherited from tests/conftest.py.
"""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.audit import AuditModule
from codeatelier_governance.loop import LoopModule


@pytest_asyncio.fixture
async def loop(audit: AuditModule) -> LoopModule:
    return LoopModule(audit)
