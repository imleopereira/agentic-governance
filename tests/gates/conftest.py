"""Shared fixtures for gates module tests.

The audit_store, secret, and audit fixtures are inherited from tests/conftest.py.
"""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.audit import AuditModule
from codeatelier_governance.gates import GatesModule


@pytest_asyncio.fixture
async def gates(audit: AuditModule, secret: bytes) -> GatesModule:
    return GatesModule(audit, secret=secret)
