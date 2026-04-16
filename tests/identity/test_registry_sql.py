"""F6 Track A: SQLAlchemy-backed registry round trips.

The production path binds ``AgentKeyRegistry`` to Postgres via an
``AsyncEngine``; in tests we exercise the engine-less in-memory mirror
(which is the fall-through path for every lookup) AND verify that the
async public surface (``register_key`` / ``lookup_key_by_fingerprint`` /
``lookup_keys_for_agent``) is reachable without an engine when the data
is already in the mirror.

A real-Postgres integration check is covered separately by the v0.6
live_test script; this unit test pins the API contract without requiring
a DB fixture (invariant #5).
"""
from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from codeatelier_governance.identity.registry import (
    AgentKeyRecord,
    AgentKeyRegistry,
)
from codeatelier_governance.identity.signer import Ed25519Signer


def _signer() -> Ed25519Signer:
    return Ed25519Signer.from_private_key(ed25519.Ed25519PrivateKey.generate())


@pytest.mark.asyncio
async def test_lookup_key_by_fingerprint_round_trip() -> None:
    reg = AgentKeyRegistry()
    signer = _signer()
    reg.register(
        key_fingerprint=signer.fingerprint,
        agent_id="prod-agent",
        public_key_pem=signer.public_key_pem,
        activated_at_chain_seq=0,
    )
    pem = await reg.lookup_key_by_fingerprint(signer.fingerprint)
    assert pem == signer.public_key_pem


@pytest.mark.asyncio
async def test_lookup_key_by_fingerprint_miss_returns_none() -> None:
    reg = AgentKeyRegistry()
    assert await reg.lookup_key_by_fingerprint("no-such-fp") is None


@pytest.mark.asyncio
async def test_lookup_keys_for_agent_returns_registered_keys() -> None:
    reg = AgentKeyRegistry()
    s1 = _signer()
    s2 = _signer()
    reg.register(
        key_fingerprint=s1.fingerprint,
        agent_id="prod-agent",
        public_key_pem=s1.public_key_pem,
        activated_at_chain_seq=0,
    )
    reg.register(
        key_fingerprint=s2.fingerprint,
        agent_id="prod-agent",
        public_key_pem=s2.public_key_pem,
        activated_at_chain_seq=100,
    )
    keys = await reg.lookup_keys_for_agent("prod-agent")
    assert len(keys) == 2
    fps = {k.key_fingerprint for k in keys}
    assert s1.fingerprint in fps and s2.fingerprint in fps
    for k in keys:
        assert isinstance(k, AgentKeyRecord)


@pytest.mark.asyncio
async def test_register_key_async_requires_engine() -> None:
    reg = AgentKeyRegistry()
    signer = _signer()
    with pytest.raises(RuntimeError, match="AsyncEngine"):
        await reg.register_key(
            agent_id="x",
            public_key_pem=signer.public_key_pem,
            activated_at_chain_seq=0,
        )
