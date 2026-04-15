"""F6 Track A — Ed25519 agent identity.

This module provides cryptographic identity for agents writing to the audit
chain. See ``decisions/2026-04-15-f6-ed25519-agent-identity-design.md`` for
the 8 non-negotiable constraints this implementation satisfies.

Public surface:
    - ``AgentIdentityConfig``          — Pydantic config with ``key_source``.
    - ``KeyStore`` protocol + three backends:
        * ``FileKeyStore``
        * ``EnvKeyStore``
        * ``EphemeralKeyStore``
    - ``Ed25519Signer`` — sign / verify over canonical audit-row bytes.
    - ``AgentKeyRegistry`` / ``RevocationStore`` — DB-backed lookup.
    - ``SignatureStatus`` — enum of per-row signing states.
"""
from __future__ import annotations

from .config import AgentIdentityConfig
from .keystore import (
    EnvKeyStore,
    EphemeralKeyStore,
    FileKeyStore,
    KeyStore,
    KeyStoreError,
)
from .registry import AgentKeyRegistry, AgentKeyRecord
from .revocation import RevocationRecord, RevocationStore
from .signer import Ed25519Signer, SignatureStatus, compute_fingerprint

__all__ = [
    "AgentIdentityConfig",
    "AgentKeyRecord",
    "AgentKeyRegistry",
    "Ed25519Signer",
    "EnvKeyStore",
    "EphemeralKeyStore",
    "FileKeyStore",
    "KeyStore",
    "KeyStoreError",
    "RevocationRecord",
    "RevocationStore",
    "SignatureStatus",
    "compute_fingerprint",
]
