"""v0.6.1 Track A finish: strict activation_seq boundary.

Per design, a single agent_id's keys must have strictly monotonic
``activated_at_chain_seq`` values. Two active keys sharing or overlapping
activation seqs produce an ambiguous ``resolve(agent_id, seq)`` at the
boundary; we reject at register time rather than let that leak into
runtime verification.
"""
from __future__ import annotations

import pytest

from codeatelier_governance.identity.registry import AgentKeyRegistry


def _pem(n: int) -> str:
    # Distinct fake PEMs keyed by n. The PEM header/footer is enough for
    # the registry: it treats ``public_key_pem`` as an opaque string.
    return f"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA{n:064x}\n-----END PUBLIC KEY-----\n"


def test_same_agent_overlap_is_rejected() -> None:
    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=100,
    )
    with pytest.raises(ValueError, match="activation_seq overlap"):
        registry.register(
            key_fingerprint="bb" * 16,
            agent_id="billing-agent",
            public_key_pem=_pem(2),
            activated_at_chain_seq=100,  # SAME seq — rejected
        )


def test_same_agent_out_of_order_is_rejected() -> None:
    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=500,
    )
    with pytest.raises(ValueError, match="strictly monotonic"):
        registry.register(
            key_fingerprint="bb" * 16,
            agent_id="billing-agent",
            public_key_pem=_pem(2),
            activated_at_chain_seq=300,  # BEFORE existing — rejected
        )


def test_different_agents_may_share_seq() -> None:
    """Overlap check is scoped per-agent; two unrelated agents are fine."""
    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=100,
    )
    registry.register(
        key_fingerprint="bb" * 16,
        agent_id="shipping-agent",
        public_key_pem=_pem(2),
        activated_at_chain_seq=100,  # same seq, DIFFERENT agent — OK
    )


def test_monotonic_rotation_is_accepted() -> None:
    """Normal key rotation: each new key activates at a strictly higher seq."""
    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=100,
    )
    registry.register(
        key_fingerprint="bb" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(2),
        activated_at_chain_seq=500,  # strictly after — OK
    )
    registry.register(
        key_fingerprint="cc" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(3),
        activated_at_chain_seq=800,
    )
    # resolve at various seqs picks the right key.
    r1 = registry.resolve("billing-agent", 200)
    r2 = registry.resolve("billing-agent", 600)
    r3 = registry.resolve("billing-agent", 900)
    assert r1 is not None and r1.key_fingerprint == "aa" * 16
    assert r2 is not None and r2.key_fingerprint == "bb" * 16
    assert r3 is not None and r3.key_fingerprint == "cc" * 16


def test_idempotent_register_same_fingerprint() -> None:
    """Re-registering IDENTICAL contents is a no-op — retries are safe."""
    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=100,
    )
    # Same fingerprint AND same contents: idempotent.
    r = registry.register(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=100,
    )
    assert r.key_fingerprint == "aa" * 16
    # And only one record exists for the agent.
    assert len(registry._by_agent["billing-agent"]) == 1


def test_cache_existing_hydrates_older_key_after_newer_is_present() -> None:
    """DA v0.6.1: caching a HISTORICAL row from the DB must not trip the
    monotonic check.

    Scenario: an agent has rotated keys. The CURRENT key (seq=500) is
    already mirrored (as you'd see after a live write). Chain verification
    later needs to look up the OLDER key (seq=100) from the DB and cache
    it — that is NOT a new registration, it is hydration of an existing
    row, and must not raise.
    """
    registry = AgentKeyRegistry()
    # Current key is in the mirror first.
    registry.register(
        key_fingerprint="bb" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(2),
        activated_at_chain_seq=500,
    )
    # Hydrating an OLDER key (seq=100) must succeed — the DB accepted it
    # long ago, we're just caching it now.
    registry._cache_existing(
        key_fingerprint="aa" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(1),
        activated_at_chain_seq=100,
    )
    # Both records are resolvable.
    assert registry.resolve("billing-agent", 200).key_fingerprint == "aa" * 16  # type: ignore[union-attr]
    assert registry.resolve("billing-agent", 600).key_fingerprint == "bb" * 16  # type: ignore[union-attr]


def test_register_still_rejects_out_of_order_after_cache_hydration() -> None:
    """``_cache_existing`` only bypasses the check for hydration — the
    public ``register`` path still enforces monotonic order."""
    registry = AgentKeyRegistry()
    registry._cache_existing(
        key_fingerprint="bb" * 16,
        agent_id="billing-agent",
        public_key_pem=_pem(2),
        activated_at_chain_seq=500,
    )
    with pytest.raises(ValueError, match="strictly monotonic"):
        registry.register(
            key_fingerprint="cc" * 16,
            agent_id="billing-agent",
            public_key_pem=_pem(3),
            activated_at_chain_seq=300,
        )
