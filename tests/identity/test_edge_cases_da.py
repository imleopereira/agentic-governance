"""F6 Track A edge cases (DA findings batch)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from codeatelier_governance.identity.keystore import (
    EnvKeyStore,
    EphemeralKeyStore,
    FileKeyStore,
)


# --- Edge case #10 ----------------------------------------------------------
def test_file_keystore_race_between_bootstrap_and_read(tmp_path: Path) -> None:
    """Two threads calling ``bootstrap_if_allowed`` concurrently must
    not corrupt the keyfile; both must observe a usable key.

    The current implementation is best-effort (the existence check is
    racy). What matters for the invariant is that a subsequent read
    returns a valid key — not that exactly one generate call won the
    race. The test asserts the observable property and pins it so a
    future refactor can see the intent.
    """
    path = tmp_path / "agent.key"
    ks_a = FileKeyStore("agent-a", path=path)
    ks_b = FileKeyStore("agent-a", path=path)
    errors: list[Exception] = []

    def _go(ks: FileKeyStore) -> None:
        try:
            ks.bootstrap_if_allowed(allow_bootstrap=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_go, args=(ks_a,))
    t2 = threading.Thread(target=_go, args=(ks_b,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, f"bootstrap raised under race: {errors}"

    # Both instances must now be able to read the same key bytes.
    k1 = ks_a.load_private_key()
    k2 = ks_b.load_private_key()
    # Exporting the raw bytes gives a deterministic comparison.
    from cryptography.hazmat.primitives import serialization
    b1 = k1.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    b2 = k2.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert b1 == b2


# --- Edge case #11 ----------------------------------------------------------
def test_two_ephemeral_keystores_produce_different_keys() -> None:
    a = EphemeralKeyStore(agent_id="x")
    b = EphemeralKeyStore(agent_id="x")
    ka = a.load_private_key()
    kb = b.load_private_key()
    from cryptography.hazmat.primitives import serialization
    raw_a = ka.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_b = kb.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert raw_a != raw_b


# --- Edge case #12 ----------------------------------------------------------
def test_env_bootstrap_process_local_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EnvKeyStore.bootstrap_if_allowed writes to os.environ in-process.

    We don't actually fork here — the test documents and pins the
    observable behavior: the env var is set in THIS process's
    os.environ and NOT propagated anywhere else.
    """
    var = "CODEATELIER_AGENT_KEY_EDGE_CASE_12_TEST"
    monkeypatch.delenv(var, raising=False)
    ks = EnvKeyStore("edge-case-12-test", var_name=var)
    ks.bootstrap_if_allowed(allow_bootstrap=True)
    # Bootstrap wrote to os.environ of THIS process only.
    import os as _os
    assert var in _os.environ
    assert _os.environ[var]  # non-empty base64 payload
    # load_private_key must succeed from the just-bootstrapped env.
    pk = ks.load_private_key()
    assert pk is not None


# --- Edge case #13 ----------------------------------------------------------
def test_verify_row_signature_with_revoked_key() -> None:
    """A revoked key's ``is_revoked_at`` rule: rows with chain_seq >=
    revoked_at_chain_seq are revoked; rows before it are not."""
    from codeatelier_governance.identity.revocation import RevocationStore

    rev = RevocationStore()
    rev.revoke(
        key_fingerprint="fp-aaaa",
        revoked_at_chain_seq=5,
        reason="operator_rotation",
        operator_id="op-1",
    )
    # Row at seq 3 (before revocation) — NOT revoked.
    assert rev.is_revoked_at("fp-aaaa", 3) is False
    # Row at seq 5 (exactly at boundary) — revoked.
    assert rev.is_revoked_at("fp-aaaa", 5) is True
    # Row at seq 7 (after) — revoked.
    assert rev.is_revoked_at("fp-aaaa", 7) is True
    # Different fingerprint — not revoked.
    assert rev.is_revoked_at("fp-bbbb", 99) is False
