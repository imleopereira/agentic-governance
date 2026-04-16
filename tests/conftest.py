"""Root-level shared fixtures for governance SDK tests.

Centralizes the audit_store + audit fixture pair that was previously
copy-pasted across 6+ module-level conftest files.

Also hosts a collection-time lint that every Pydantic response model in
``codeatelier_governance.console.models.responses`` declares
``extra='forbid'``. The lint runs at import time so it fires even when no
test happens to import the models module.
"""
from __future__ import annotations

import inspect
import secrets as _secrets
from typing import AsyncIterator

import pytest_asyncio
from pydantic import BaseModel

from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)


def _enforce_forbid_extra_on_response_models() -> None:
    """Collection-time lint: every response model must forbid extras."""
    from codeatelier_governance.console.models import responses as _responses

    offenders: list[str] = []
    for name, obj in inspect.getmembers(_responses, inspect.isclass):
        if not issubclass(obj, BaseModel) or obj is BaseModel:
            continue
        cfg = obj.model_config
        if cfg.get("extra") != "forbid" or cfg.get("strict") is not True:
            offenders.append(name)
    if offenders:
        raise RuntimeError(
            "The following console response models must declare "
            "model_config=ConfigDict(extra='forbid', strict=True): "
            + ", ".join(sorted(offenders))
        )


_enforce_forbid_extra_on_response_models()


@pytest_asyncio.fixture
async def audit_store() -> InMemoryAuditStore:
    """Shared in-memory audit store."""
    return InMemoryAuditStore(max_events=10_000)


@pytest_asyncio.fixture
async def secret() -> bytes:
    """Random 32-byte HMAC secret for tests."""
    return _secrets.token_bytes(32)


@pytest_asyncio.fixture
async def audit(
    audit_store: InMemoryAuditStore, secret: bytes
) -> AsyncIterator[AuditModule]:
    """Fully wired AuditModule with batching writer, started and torn down."""
    writer = BatchingWriter(
        primary=audit_store,
        batch_size=5,
        flush_interval_s=0.02,
        buffer_max=1000,
    )
    module = AuditModule(audit_store, secret=secret, writer=writer)
    await module.start()
    try:
        yield module
    finally:
        await module.close()
