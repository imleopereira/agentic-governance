"""EnvKeyStore — base64 round-trip, missing var, malformed data."""
from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from codeatelier_governance.identity.keystore import EnvKeyStore, KeyStoreError


def _pem_b64() -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode("ascii")


def test_roundtrip_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEATELIER_AGENT_KEY_FOO_BAR", _pem_b64())
    store = EnvKeyStore("foo-bar")
    priv = store.load_private_key()
    sig = priv.sign(b"x")
    store.load_public_key().verify(sig, b"x")


def test_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEATELIER_AGENT_KEY_MISSING", raising=False)
    store = EnvKeyStore("missing")
    with pytest.raises(KeyStoreError, match="not set"):
        store.load_private_key()


def test_invalid_base64_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEATELIER_AGENT_KEY_BAD", "!!!not-base64!!!")
    store = EnvKeyStore("bad")
    with pytest.raises(KeyStoreError, match="base64"):
        store.load_private_key()


def test_invalid_pem_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CODEATELIER_AGENT_KEY_BAD2",
        base64.b64encode(b"not a pem").decode("ascii"),
    )
    store = EnvKeyStore("bad2")
    with pytest.raises(KeyStoreError, match="PEM"):
        store.load_private_key()


def test_custom_var_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_CUSTOM_VAR", _pem_b64())
    store = EnvKeyStore("whatever", var_name="MY_CUSTOM_VAR")
    assert store.load_private_key() is not None
