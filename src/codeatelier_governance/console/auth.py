"""Console authentication: password hashing, sessions, and role helpers.

Uses stdlib PBKDF2-HMAC-SHA256 for password hashing to avoid adding a
new dependency (passlib/bcrypt). PBKDF2 with 600k iterations is OWASP's
current recommendation for SHA-256.

No external dependencies beyond the standard library.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID, uuid4

# OWASP 2023 recommendation for PBKDF2-HMAC-SHA256
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 32


class Role(str, Enum):
    """Console user roles."""

    VIEWER = "viewer"
    ADMIN = "admin"


def hash_password(plain: str) -> str:
    """Hash a plaintext password with PBKDF2-HMAC-SHA256.

    Returns a string in the format: ``pbkdf2:iterations:salt_hex:hash_hex``
    """
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2:{_PBKDF2_ITERATIONS}:{salt.hex()}:{dk.hex()}"


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored hash.

    Returns True if the password matches, False otherwise.
    Uses constant-time comparison to prevent timing attacks.
    """
    try:
        parts = hashed.split(":")
        if len(parts) != 4 or parts[0] != "pbkdf2":
            return False
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        stored_hash = bytes.fromhex(parts[3])
    except (ValueError, IndexError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, iterations
    )
    return secrets.compare_digest(dk, stored_hash)


def create_session_id() -> UUID:
    """Generate a cryptographically random session ID."""
    return uuid4()


def session_expires_at(ttl_hours: int = 8) -> datetime:
    """Compute session expiry timestamp."""
    return datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
