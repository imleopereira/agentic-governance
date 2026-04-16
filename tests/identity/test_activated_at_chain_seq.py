"""BLOCKER 2: ``activated_at_chain_seq`` must reflect the CURRENT head.

Previously ``_build_identity`` hardcoded ``activated_at_chain_seq=0``
when registering the ephemeral key, which broke Track B lookups that
span a non-empty chain combined with Ed25519 identity registration.
The fix defers registration to ``start()`` and queries
``store.get_current_chain_seq()``.
"""
from __future__ import annotations

import secrets as _secrets
from unittest.mock import AsyncMock

import pytest

from codeatelier_governance import GovernanceSDK


FAKE_URL = "postgresql://fake:fake@localhost:1/fake"


def _sdk() -> GovernanceSDK:
    return GovernanceSDK(
        database_url=FAKE_URL,
        audit_secret=_secrets.token_bytes(32),
    )


@pytest.mark.asyncio
async def test_activated_at_chain_seq_uses_current_head_not_zero() -> None:
    sdk = _sdk()
    assert sdk._identity_signer is not None  # pytest auto-enables ephemeral
    assert sdk._identity_registry is not None

    # Stub the store's get_current_chain_seq to simulate 50 prior rows.
    fake_getter = AsyncMock(return_value=50)
    sdk.audit._store.get_current_chain_seq = fake_getter  # type: ignore[attr-defined]

    await sdk._register_identity_key()

    fp = sdk._identity_signer.fingerprint
    record = sdk._identity_registry.get_by_fingerprint(fp)
    assert record is not None
    assert record.activated_at_chain_seq == 50


@pytest.mark.asyncio
async def test_activated_at_chain_seq_zero_on_empty_chain() -> None:
    sdk = _sdk()
    assert sdk._identity_signer is not None

    fake_getter = AsyncMock(return_value=0)
    sdk.audit._store.get_current_chain_seq = fake_getter  # type: ignore[attr-defined]

    await sdk._register_identity_key()

    fp = sdk._identity_signer.fingerprint
    record = sdk._identity_registry.get_by_fingerprint(fp)
    assert record is not None
    assert record.activated_at_chain_seq == 0


@pytest.mark.asyncio
async def test_activated_at_chain_seq_falls_back_when_db_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invariant #1: SDK must still start when the audit store DB is down.

    The registration is deferred (NOT stamped with bogus data) and a WARN
    is logged.
    """
    sdk = _sdk()
    assert sdk._identity_signer is not None

    raising = AsyncMock(side_effect=RuntimeError("DB down"))
    sdk.audit._store.get_current_chain_seq = raising  # type: ignore[attr-defined]

    # Must not raise.
    await sdk._register_identity_key()

    fp = sdk._identity_signer.fingerprint
    # Key is NOT registered because registration was deferred.
    assert sdk._identity_registry.get_by_fingerprint(fp) is None


@pytest.mark.asyncio
async def test_in_memory_store_without_getter_defaults_to_zero() -> None:
    """In-memory stores have no ``get_current_chain_seq`` method; the
    registration path should default to 0 rather than crash."""
    sdk = _sdk()
    # Force the helper to be missing by shadowing with None at the
    # instance level (can't delattr a class-defined method).
    sdk.audit._store = object.__new__(type("StubStore", (), {}))  # type: ignore[attr-defined]
    await sdk._register_identity_key()
    fp = sdk._identity_signer.fingerprint
    rec = sdk._identity_registry.get_by_fingerprint(fp)
    assert rec is not None
    assert rec.activated_at_chain_seq == 0
