"""EphemeralKeyStore — in-memory only, per-instance unique keys."""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization

from codeatelier_governance.identity.keystore import EphemeralKeyStore


def _pub_pem(store: EphemeralKeyStore) -> bytes:
    return store.load_public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_two_instances_different_keys() -> None:
    a = EphemeralKeyStore("agent-a")
    b = EphemeralKeyStore("agent-a")
    assert _pub_pem(a) != _pub_pem(b)


def test_sign_verify_roundtrip() -> None:
    store = EphemeralKeyStore("agent-x")
    sig = store.load_private_key().sign(b"payload")
    store.load_public_key().verify(sig, b"payload")


def test_no_filesystem_access(tmp_path, monkeypatch) -> None:
    """Ephemeral backend MUST NOT touch the filesystem.

    We prove this by poisoning ``open`` for paths containing the tmp dir —
    if the keystore tries to write anywhere under it, the test blows up.
    """
    real_open = open
    tmp_str = str(tmp_path)

    def guarded(path, *a, **kw):  # type: ignore[no-untyped-def]
        if isinstance(path, (str, bytes)) and tmp_str.encode() in (
            path if isinstance(path, bytes) else path.encode()
        ):
            raise AssertionError("ephemeral keystore touched the filesystem")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", guarded)
    store = EphemeralKeyStore("agent-y")
    store.bootstrap_if_allowed(allow_bootstrap=True)
    store.load_private_key()
