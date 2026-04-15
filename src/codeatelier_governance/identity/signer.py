"""Ed25519 signer over canonical audit-row bytes.

The canonicalization contract is the caller's responsibility: the signer
accepts opaque ``bytes`` and produces a 64-byte Ed25519 signature.  Callers
MUST use the same serialization on sign and verify paths — typically the
canonical JSON produced by ``audit.chain._canonical``.

Fingerprint choice: ``sha256(public_key_pem)[:32]`` is used here because
PEM-encoded Ed25519 public keys have negligible dictionary risk — there is
no low-entropy "common key" to collide on.  This differs from the HMAC
chain-key fingerprint (which uses an HMAC construction because the secret
keying material would otherwise be guessable).  See design doc section 11.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


class SignatureStatus(str, enum.Enum):
    """Per-row signing state, persisted as ``signature_status`` in Postgres."""

    SIGNED = "signed"
    UNSIGNED = "unsigned"
    UNSIGNED_LOCAL_FAILURE = "unsigned_local_failure"
    LEGACY_UNSIGNED = "legacy_unsigned"
    REVOKED_KEY = "revoked_key"
    INVALID_SIGNATURE = "invalid_signature"
    UNKNOWN_KEY = "unknown_key"


def compute_fingerprint(public_key_pem: str | bytes) -> str:
    """Stable fingerprint of an Ed25519 public key.

    ``sha256(public_key_pem)[:32]`` hex-encoded — 32 hex chars.  PEM is used
    (not DER) so operators can eyeball-match the fingerprint against a
    ``ssh-keygen -lf`` style output if they dump the PEM.
    """
    data = public_key_pem.encode("utf-8") if isinstance(public_key_pem, str) else public_key_pem
    return hashlib.sha256(data).hexdigest()[:32]


@dataclass
class Ed25519Signer:
    """Thin wrapper around PyCA's ``Ed25519PrivateKey`` + public-key cache.

    The signer holds the private key in memory for the lifetime of the
    process.  Instantiate via one of the keystores rather than constructing
    directly unless you already have a PyCA key object.
    """

    private_key: ed25519.Ed25519PrivateKey
    public_key_pem: str

    @classmethod
    def from_private_key(
        cls, private_key: ed25519.Ed25519PrivateKey
    ) -> "Ed25519Signer":
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        return cls(private_key=private_key, public_key_pem=public_pem)

    @property
    def fingerprint(self) -> str:
        return compute_fingerprint(self.public_key_pem)

    def sign(self, canonical_row_bytes: bytes) -> bytes:
        """Produce the raw 64-byte Ed25519 signature."""
        return self.private_key.sign(canonical_row_bytes)

    @staticmethod
    def verify(
        canonical_row_bytes: bytes,
        signature: bytes,
        public_key_pem: str | bytes,
    ) -> bool:
        """Return True iff the signature verifies under the given public key.

        Never raises — callers generally want a boolean.  An invalid
        signature, a malformed PEM, or a wrong-type key all return False.
        """
        try:
            pem_bytes = (
                public_key_pem.encode("utf-8")
                if isinstance(public_key_pem, str)
                else public_key_pem
            )
            pub = serialization.load_pem_public_key(pem_bytes)
            if not isinstance(pub, ed25519.Ed25519PublicKey):
                return False
            pub.verify(signature, canonical_row_bytes)
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False
