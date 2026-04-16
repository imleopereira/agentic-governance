"""Ed25519Signer — sign/verify round-trip, tampering, wrong key."""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import ed25519

from codeatelier_governance.identity.signer import (
    Ed25519Signer,
    compute_fingerprint,
)


def _signer() -> Ed25519Signer:
    return Ed25519Signer.from_private_key(ed25519.Ed25519PrivateKey.generate())


def test_sign_verify_roundtrip() -> None:
    s = _signer()
    sig = s.sign(b"canonical-row-bytes")
    assert Ed25519Signer.verify(b"canonical-row-bytes", sig, s.public_key_pem)


def test_tampered_payload_fails() -> None:
    s = _signer()
    sig = s.sign(b"canonical-row-bytes")
    assert not Ed25519Signer.verify(b"canonical-row-BYTES", sig, s.public_key_pem)


def test_wrong_public_key_fails() -> None:
    s1 = _signer()
    s2 = _signer()
    sig = s1.sign(b"abc")
    assert not Ed25519Signer.verify(b"abc", sig, s2.public_key_pem)


def test_malformed_public_key_returns_false() -> None:
    s = _signer()
    sig = s.sign(b"abc")
    assert not Ed25519Signer.verify(b"abc", sig, "not a pem")


def test_fingerprint_stable_and_length() -> None:
    s = _signer()
    fp1 = s.fingerprint
    fp2 = compute_fingerprint(s.public_key_pem)
    assert fp1 == fp2
    assert len(fp1) == 32
    assert all(c in "0123456789abcdef" for c in fp1)
