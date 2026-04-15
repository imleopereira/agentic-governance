"""Unit tests for the F9 in-memory wrapper registry."""
from __future__ import annotations

import uuid

import pytest

from codeatelier_governance.coverage.registry import (
    WrapperRegistry,
    compute_hostname_hash,
)


def test_register_fire_and_forget_never_raises() -> None:
    reg = WrapperRegistry(salt=b"x" * 32)
    # With engine=None, flush must be a no-op and must not raise.
    r = reg.register("agent-a", "openai")
    assert r.agent_id == "agent-a"
    assert r.provider == "openai"
    assert r.instance_id == reg.instance_id


def test_register_is_idempotent_per_key() -> None:
    reg = WrapperRegistry(salt=b"x" * 32)
    first = reg.register("agent-a", "openai")
    second = reg.register("agent-a", "openai")
    # Same (agent_id, provider, instance_id) triple — single row, updated.
    assert len(reg.snapshot()) == 1
    assert first is second or first.agent_id == second.agent_id


def test_instance_id_unique_across_instances() -> None:
    a = WrapperRegistry(salt=b"x" * 32)
    b = WrapperRegistry(salt=b"x" * 32)
    assert a.instance_id != b.instance_id
    assert isinstance(a.instance_id, uuid.UUID)


def test_hostname_hash_deterministic() -> None:
    salt = b"s" * 32
    h1 = compute_hostname_hash("alice-mbp.local", salt=salt)
    h2 = compute_hostname_hash("alice-mbp.local", salt=salt)
    assert h1 == h2
    # Different salt → different hash.
    h3 = compute_hostname_hash("alice-mbp.local", salt=b"t" * 32)
    assert h1 != h3
    # Raw hostname is NOT present in the hash output.
    assert "alice" not in h1


def test_hostname_hash_not_raw_hostname() -> None:
    reg = WrapperRegistry(salt=b"x" * 32)
    r = reg.register("agent-a", "openai")
    assert r.hostname_hash != ""
    assert " " not in r.hostname_hash
    # Should look like a hex digest.
    assert len(r.hostname_hash) == 64
    int(r.hostname_hash, 16)  # raises if not hex


@pytest.mark.asyncio
async def test_flush_with_none_engine_is_noop() -> None:
    reg = WrapperRegistry(salt=b"x" * 32)
    reg.register("agent-a", "openai")
    # Must not raise.
    await reg.flush_to_postgres(None)


@pytest.mark.asyncio
async def test_flush_swallows_exceptions() -> None:
    reg = WrapperRegistry(salt=b"x" * 32)
    reg.register("agent-a", "openai")

    class BrokenEngine:
        def begin(self) -> "BrokenEngine":
            raise RuntimeError("DB down")

    # Must not raise — invariant #1.
    await reg.flush_to_postgres(BrokenEngine())
