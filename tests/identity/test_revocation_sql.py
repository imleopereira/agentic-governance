"""F6 Track A: revocation store — forward-looking semantics + append-only.

The SQLAlchemy engine path is exercised by the v0.6 live_test against
real Postgres; this unit test pins the ``is_revoked_at_seq`` semantics
and the append-only invariant without a DB fixture (invariant #5).
"""
from __future__ import annotations

import pytest

from codeatelier_governance.identity.revocation import RevocationStore


@pytest.mark.asyncio
async def test_revoke_and_query_is_revoked_at_seq() -> None:
    store = RevocationStore()
    store.revoke(
        key_fingerprint="fpA",
        revoked_at_chain_seq=100,
        reason="leaked",
        operator_id="op-1",
    )
    # Rows BEFORE revocation seq are still valid.
    assert await store.is_revoked_at_seq("fpA", 50) is False
    # Row AT the revocation seq is considered revoked (fires at that seq).
    assert await store.is_revoked_at_seq("fpA", 100) is True
    # Rows AFTER are revoked.
    assert await store.is_revoked_at_seq("fpA", 200) is True
    # Unknown fingerprints are never revoked.
    assert await store.is_revoked_at_seq("fpB", 1_000_000) is False


def test_revocation_store_is_append_only() -> None:
    """Constraint #4 application-level enforcement: no UPDATE / DELETE API.

    Introspect the class to assert no mutation methods leak in. We allow
    ``revoke`` / ``revoke_async`` (append) but reject any method whose
    name suggests removal or mutation of an existing row.
    """
    forbidden_substrings = (
        "delete",
        "remove",
        "pop",
        "unrevoke",
        "update",
        "clear",
        "truncate",
    )
    public_methods = [
        name for name in dir(RevocationStore) if not name.startswith("_")
    ]
    for name in public_methods:
        for bad in forbidden_substrings:
            assert bad not in name.lower(), (
                f"RevocationStore.{name} looks like a mutation/removal "
                f"method; revocations are append-only (constraint #4)."
            )


@pytest.mark.asyncio
async def test_revoke_async_requires_engine() -> None:
    store = RevocationStore()
    with pytest.raises(RuntimeError, match="AsyncEngine"):
        await store.revoke_async(
            fingerprint="fp",
            revoked_at_chain_seq=1,
            reason="r",
            operator_id="op",
        )
