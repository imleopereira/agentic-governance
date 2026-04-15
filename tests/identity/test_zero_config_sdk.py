"""Zero-config SDK instantiation auto-wires ephemeral Ed25519 signing.

Invariant #5: every public class is instantiable with zero config in
tests. Combined with constraint #3 (ephemeral test-mode default), this
means ``GovernanceSDK(database_url=...)`` under pytest auto-builds an
``EphemeralKeyStore``, a signer, and wires it into ``sdk.audit`` — audit
writes succeed and rows carry ``signature_status='signed'``.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit.models import AuditEvent


@pytest.mark.asyncio
async def test_zero_config_sdk_with_inmemory_store_signs_rows() -> None:
    import secrets as _secrets

    # Use a real random secret to pass the strength check.
    async with GovernanceSDK(
        database_url="postgresql://fake:fake@localhost:1/fake",
        audit_secret=_secrets.token_bytes(32),
    ) as sdk:
        # We don't actually hit Postgres in this test — we exercise the
        # ephemeral-signer wiring via the audit path, which under degraded
        # primary will spill to the JSONL fallback. The signature state is
        # attached BEFORE the write attempt, so this assertion is valid
        # regardless of which backend stores the row.
        session_id = uuid4()
        record = await sdk.audit.log(
            AuditEvent(
                agent_id="zero-config-agent",
                kind="tool.call",
                session_id=session_id,
            )
        )
        # Zero-config ephemeral signer was built and wired.
        assert record.signature_status == "signed", (
            f"expected signed, got {record.signature_status}"
        )
        assert record.signature is not None
        assert len(record.signature) == 64
        assert record.signing_key_fingerprint is not None
