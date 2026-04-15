"""CONSTRAINT #3 + #5 — zero-config instantiation in test mode.

Under pytest (``PYTEST_CURRENT_TEST`` is set automatically), the
EphemeralKeyStore must be selectable with NO arguments and the full
sign/verify path must work end-to-end without any fixture setup.

We test via the identity building blocks directly rather than through
the full ``GovernanceSDK()`` because the SDK wiring into AuditModule
is intentionally deferred (see Track A report for the follow-up).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from codeatelier_governance.audit.chain import (
    sign_audit_row,
    verify_audit_row_signature,
)
from codeatelier_governance.identity.config import AgentIdentityConfig
from codeatelier_governance.identity.keystore import (
    EphemeralKeyStore,
    build_keystore,
    is_test_mode,
)
from codeatelier_governance.identity.registry import AgentKeyRegistry
from codeatelier_governance.identity.signer import Ed25519Signer


def test_is_test_mode_true_under_pytest() -> None:
    assert is_test_mode() is True


def test_zero_config_identity_config_defaults() -> None:
    cfg = AgentIdentityConfig()
    assert cfg.enabled is False
    assert cfg.key_source == "ephemeral"
    assert cfg.allow_bootstrap is False
    assert cfg.key_uri is None


def test_extra_forbid_on_config() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentIdentityConfig(typo_field=True)  # type: ignore[call-arg]


def test_end_to_end_with_ephemeral_backend() -> None:
    ks = build_keystore("agent-z", "ephemeral")
    assert isinstance(ks, EphemeralKeyStore)
    signer = Ed25519Signer.from_private_key(ks.load_private_key())

    registry = AgentKeyRegistry()
    registry.register(
        key_fingerprint=signer.fingerprint,
        agent_id="agent-z",
        public_key_pem=signer.public_key_pem,
        activated_at_chain_seq=0,
    )

    row = {
        "event_id": uuid4(),
        "session_id": uuid4(),
        "agent_id": "agent-z",
        "parent_event_id": None,
        "kind": "tool.call",
        "model": None,
        "input_hash": None,
        "output_hash": None,
        "metadata": {"k": "v"},
        "prev_hash": None,
        "created_at": datetime.now(timezone.utc),
        "hmac": "b" * 64,
        "chain_seq": 1,
    }
    sig, fp, status = sign_audit_row(row, signer)
    assert status == "signed"
    assert verify_audit_row_signature(row, sig, fp, registry) == "signed"
