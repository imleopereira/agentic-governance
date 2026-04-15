"""F6 Track A wiring edge cases (DA findings batch).

Covers:
    #24 Chain linkage survives signer failure mid-session
    #25 JsonlFallbackStore deserialize drops signature gracefully
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from codeatelier_governance.audit.jsonl_store import (
    JsonlFallbackStore,
    _deserialize,
)
from codeatelier_governance.audit.models import AuditEvent, AuditEventRecord
from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.audit.store import InMemoryAuditStore
from codeatelier_governance.identity.signer import Ed25519Signer


# --- Edge case #24 ----------------------------------------------------------
@pytest.mark.asyncio
async def test_chain_linkage_survives_signer_failure_mid_session() -> None:
    """HMAC ``prev_hash`` chain must stay monotonic even when the signer
    raises for some rows in the middle of a session.

    Constructs a wrapper signer that fails row N then recovers.
    """
    real_signer = Ed25519Signer.from_private_key(
        ed25519.Ed25519PrivateKey.generate()
    )

    class _FlakySigner:
        def __init__(self, inner: Ed25519Signer) -> None:
            self._inner = inner
            self.call_count = 0
            self.fail_on = {6}  # row 6 fails

        @property
        def fingerprint(self) -> str:
            return self._inner.fingerprint

        @property
        def public_key_pem(self) -> str:
            return self._inner.public_key_pem

        def sign(self, data: bytes) -> bytes:
            self.call_count += 1
            if self.call_count in self.fail_on:
                raise RuntimeError(f"signer fault at call {self.call_count}")
            return self._inner.sign(data)

    store = InMemoryAuditStore()
    audit = AuditModule(
        store,
        secret=secrets.token_bytes(32),
        signer=_FlakySigner(real_signer),
    )
    await audit.start()
    try:
        session_id = uuid4()
        for i in range(10):
            await audit.log(
                AuditEvent(
                    agent_id="chain-survive-test",
                    kind=f"tool.call.{i}",
                    session_id=session_id,
                )
            )
        events = await store.get_session_events(session_id)
        assert len(events) == 10

        # Rows before and after the failure must still chain correctly:
        # prev_hash linkage is unbroken across the signing gap.
        for idx in range(1, len(events)):
            assert events[idx].prev_hash == events[idx - 1].hmac, (
                f"chain linkage broken between rows {idx - 1} and {idx} "
                f"(signer failure gap)"
            )

        # The failing row has signature_status='unsigned_local_failure'.
        statuses = [r.signature_status for r in events]
        assert "unsigned_local_failure" in statuses
        # Rows after the fault recovered to 'signed'.
        assert statuses[-1] == "signed"
    finally:
        await audit.close()


# --- Edge case #25 ----------------------------------------------------------
@pytest.mark.asyncio
async def test_jsonl_fallback_deserialize_drops_signature_gracefully(
    tmp_path: Path,
) -> None:
    """Rows roundtripped through JsonlFallbackStore lose their Ed25519
    signature fields (the serializer does not persist them).

    Downstream verification must treat this as 'unsigned' rather than
    crash on the missing fields — this is the documented degradation
    path from the Track A wiring report.
    """
    # Build a fully-signed record in-memory.
    signer = Ed25519Signer.from_private_key(
        ed25519.Ed25519PrivateKey.generate()
    )
    record = AuditEventRecord(
        event_id=uuid4(),
        session_id=uuid4(),
        agent_id="fallback-deserialize",
        parent_event_id=None,
        kind="tool.call",
        input_hash=None,
        output_hash=None,
        metadata={},
        prev_hash=None,
        hmac="a" * 64,
        created_at=datetime.now(timezone.utc),
        signature=b"\x01" * 64,
        signing_key_fingerprint=signer.fingerprint,
        signature_status="signed",
    )

    # Append + read back through the JSONL path.
    path = tmp_path / "fb.jsonl"
    store = JsonlFallbackStore(path)
    await store._append(record)  # type: ignore[attr-defined]

    raw_line = path.read_text().splitlines()[0]
    data = json.loads(raw_line)
    # The serializer does not persist signature/fingerprint/status.
    assert "signature" not in data
    assert "signing_key_fingerprint" not in data

    # Deserialize must not crash and must produce a record with
    # signature=None and the default 'unsigned' status.
    reparsed = _deserialize(data)
    assert reparsed.signature is None
    assert reparsed.signing_key_fingerprint is None
    assert reparsed.signature_status == "unsigned"
