"""Shared fixtures for presence module tests."""
from __future__ import annotations

import pytest_asyncio

from codeatelier_governance.presence import PresenceModule


@pytest_asyncio.fixture
async def presence() -> PresenceModule:
    return PresenceModule()
