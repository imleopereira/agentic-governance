"""v0.6.2 P0 — verify_session_chain must be rotation-aware.

Pre-fix, ``GET /api/session/{id}/verify`` called ``verify_event(record,
AUDIT_SECRET)`` against every row in the session under the CURRENT
secret only. Any customer who ran ``cga rotate-chain-key`` would then
see ``verified=false`` for every pre-rotation event in the UI session
drawer — legitimate, unaltered rows mis-flagged as tampered. This is
the same bug that bit the v0.6.2 demo seed before rotation was dropped
from it. The compliance-report verifier already handles rotation via
``verify_chain_with_rotation``; the per-session drawer verifier did
not. Fix: load ``governance_audit_chain_keys`` in the endpoint, pick
the key active at each row's chain_seq, and verify under THAT key.

These tests drive the handler directly (no HTTP layer) with mocked
engine connections, asserting the endpoint:
    * empty key_versions → legacy single-key path (back-compat)
    * rows pre-rotation verify under outgoing secret
    * rows post-rotation verify under incoming secret
    * missing historical key material → verified=False (not raised)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from codeatelier_governance.audit.keys import fingerprint_key


def _hmac_row(
    *,
    secret: bytes,
    event_id: UUID,
    session_id: UUID,
    agent_id: str,
    parent_event_id: UUID | None,
    kind: str,
    metadata: dict[str, Any],
    prev_hash: str | None,
    created_at: datetime,
) -> str:
    """Compute the canonical HMAC for an audit row using the real helper."""
    from codeatelier_governance.audit.chain import compute_event_hmac

    return compute_event_hmac(
        secret=secret,
        event_id=event_id,
        session_id=session_id,
        agent_id=agent_id,
        parent_event_id=parent_event_id,
        kind=kind,
        input_hash=None,
        output_hash=None,
        metadata=metadata,
        prev_hash=prev_hash,
        created_at=created_at,
    )


def _build_engine_mock(rows: list[dict[str, Any]], key_versions: list[dict[str, Any]]) -> Any:
    """Mock AsyncEngine returning session rows + chain_keys rows in order."""

    async def mock_execute(stmt: Any, params: Any = None) -> Any:
        result = MagicMock()
        sql = str(stmt.text) if hasattr(stmt, "text") else str(stmt)
        if "governance_audit_events" in sql and "session_id" in sql:
            result.mappings.return_value = rows
        elif "governance_audit_chain_keys" in sql:
            result.mappings.return_value = key_versions
        else:
            result.mappings.return_value = []
        return result

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock(side_effect=mock_execute)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=None)

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_ctx)
    return mock_engine


@pytest.mark.asyncio
async def test_empty_key_versions_uses_legacy_single_key_path() -> None:
    """No rows in governance_audit_chain_keys → legacy verify under AUDIT_SECRET.

    Back-compat: deployments that never rotated (or skipped the F6
    Track B migration) must behave identically to pre-fix.
    """
    from codeatelier_governance.console import app as console_app

    secret = b"x" * 32
    session_id = uuid4()
    event_id = uuid4()
    created_at = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)
    mac = _hmac_row(
        secret=secret,
        event_id=event_id,
        session_id=session_id,
        agent_id="agent-a",
        parent_event_id=None,
        kind="scope.ok",
        metadata={},
        prev_hash=None,
        created_at=created_at,
    )
    rows = [
        {
            "chain_seq": 1,
            "event_id": event_id,
            "session_id": session_id,
            "agent_id": "agent-a",
            "parent_event_id": None,
            "kind": "scope.ok",
            "model": None,
            "input_hash": None,
            "output_hash": None,
            "metadata_json": {},
            "prev_hash": None,
            "hmac_value": mac,
            "hmac_next": None,
            "created_at": created_at,
        }
    ]
    engine = _build_engine_mock(rows, key_versions=[])

    with patch.object(console_app, "AUDIT_SECRET", "x" * 32), patch.object(
        console_app, "engine", engine
    ):
        result = await console_app.verify_session_chain(session_id)

    assert result["verified"] is True
    assert result["event_count"] == 1
    assert result["events"][0]["verified"] is True


@pytest.mark.asyncio
async def test_rotation_spanning_session_verifies_under_correct_keys() -> None:
    """Session has pre-rotation + post-rotation events. Both verify.

    Pre-fix, only the post-rotation events (signed under the current
    AUDIT_SECRET) would verify. This test pins the fix.
    """
    import base64
    import os

    from codeatelier_governance.console import app as console_app

    outgoing_secret = b"outgoing-secret-is-32-bytes-long"
    incoming_secret = b"incoming-secret-is-32-bytes-long"
    assert len(outgoing_secret) == 32 and len(incoming_secret) == 32

    session_id = uuid4()
    pre_event_id = uuid4()
    post_event_id = uuid4()
    created_at = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)

    # chain_seq=10: pre-rotation, signed under outgoing
    pre_mac = _hmac_row(
        secret=outgoing_secret,
        event_id=pre_event_id,
        session_id=session_id,
        agent_id="agent-a",
        parent_event_id=None,
        kind="scope.ok",
        metadata={},
        prev_hash=None,
        created_at=created_at,
    )
    # chain_seq=20: post-rotation, signed under incoming (the current AUDIT_SECRET)
    post_mac = _hmac_row(
        secret=incoming_secret,
        event_id=post_event_id,
        session_id=session_id,
        agent_id="agent-a",
        parent_event_id=None,
        kind="cost.ok",
        metadata={},
        prev_hash=None,
        created_at=created_at,
    )

    rows = [
        {
            "chain_seq": 10,
            "event_id": pre_event_id,
            "session_id": session_id,
            "agent_id": "agent-a",
            "parent_event_id": None,
            "kind": "scope.ok",
            "model": None,
            "input_hash": None,
            "output_hash": None,
            "metadata_json": {},
            "prev_hash": None,
            "hmac_value": pre_mac,
            "hmac_next": None,
            "created_at": created_at,
        },
        {
            "chain_seq": 20,
            "event_id": post_event_id,
            "session_id": session_id,
            "agent_id": "agent-a",
            "parent_event_id": None,
            "kind": "cost.ok",
            "model": None,
            "input_hash": None,
            "output_hash": None,
            "metadata_json": {},
            "prev_hash": None,
            "hmac_value": post_mac,
            "hmac_next": None,
            "created_at": created_at,
        },
    ]

    outgoing_fp = fingerprint_key(outgoing_secret)
    incoming_fp = fingerprint_key(incoming_secret)

    key_versions = [
        {
            "key_version": 1,
            "fingerprint": outgoing_fp,
            "activated_at_chain_seq": 1,
            "retired_at_chain_seq": 15,  # outgoing covers 1..14
        },
        {
            "key_version": 2,
            "fingerprint": incoming_fp,
            "activated_at_chain_seq": 15,
            "retired_at_chain_seq": None,  # current
        },
    ]

    engine = _build_engine_mock(rows, key_versions=key_versions)
    # Current AUDIT_SECRET = incoming; operator provides outgoing via
    # GOVERNANCE_CHAIN_KEY_<fp16> -> env://<NAME>, where <NAME> is
    # base64-encoded key material (see audit.keys._resolve_uri).
    env_name = f"GOVERNANCE_CHAIN_KEY_{outgoing_fp[:16]}"
    os.environ[env_name] = "env://_TEST_OUTGOING_RAW_b64"
    os.environ["_TEST_OUTGOING_RAW_b64"] = base64.b64encode(outgoing_secret).decode("ascii")

    from codeatelier_governance.audit.keys import clear_key_cache

    clear_key_cache()

    try:
        with patch.object(
            console_app, "AUDIT_SECRET", incoming_secret.decode("utf-8")
        ), patch.object(console_app, "engine", engine):
            result = await console_app.verify_session_chain(session_id)
    finally:
        os.environ.pop(env_name, None)
        os.environ.pop("_TEST_OUTGOING_RAW_b64", None)
        clear_key_cache()

    assert result["event_count"] == 2
    # Critical assertion: both rows verified despite spanning rotation.
    # Pre-fix, the first would be verified=False because AUDIT_SECRET !=
    # outgoing_secret.
    assert result["events"][0]["verified"] is True, (
        "pre-rotation event should verify under outgoing key; if this fails, "
        "verify_session_chain regressed to single-key mode"
    )
    assert result["events"][1]["verified"] is True
    assert result["verified"] is True


@pytest.mark.asyncio
async def test_missing_historical_key_material_surfaces_as_unverified() -> None:
    """Operator hasn't mounted the outgoing env var → verified=False, no crash.

    Pre-rotation events should be False (can't verify) but the handler
    must not raise — the host app shouldn't break on a missing key.
    """
    from codeatelier_governance.console import app as console_app

    session_id = uuid4()
    pre_event_id = uuid4()
    created_at = datetime(2026, 4, 16, 12, 0, 0, tzinfo=timezone.utc)

    outgoing_secret = b"outgoing-secret-is-32-bytes-long"
    pre_mac = _hmac_row(
        secret=outgoing_secret,
        event_id=pre_event_id,
        session_id=session_id,
        agent_id="agent-a",
        parent_event_id=None,
        kind="scope.ok",
        metadata={},
        prev_hash=None,
        created_at=created_at,
    )

    rows = [
        {
            "chain_seq": 10,
            "event_id": pre_event_id,
            "session_id": session_id,
            "agent_id": "agent-a",
            "parent_event_id": None,
            "kind": "scope.ok",
            "model": None,
            "input_hash": None,
            "output_hash": None,
            "metadata_json": {},
            "prev_hash": None,
            "hmac_value": pre_mac,
            "hmac_next": None,
            "created_at": created_at,
        }
    ]

    outgoing_fp = fingerprint_key(outgoing_secret)
    incoming_fp = fingerprint_key(b"current-secret-is-32-bytes-long!")
    key_versions = [
        {
            "key_version": 1,
            "fingerprint": outgoing_fp,
            "activated_at_chain_seq": 1,
            "retired_at_chain_seq": 15,
        },
        {
            "key_version": 2,
            "fingerprint": incoming_fp,
            "activated_at_chain_seq": 15,
            "retired_at_chain_seq": None,
        },
    ]

    engine = _build_engine_mock(rows, key_versions=key_versions)

    from codeatelier_governance.audit.keys import clear_key_cache

    clear_key_cache()

    # Operator did NOT set GOVERNANCE_CHAIN_KEY_<outgoing_fp[:16]>.
    with patch.object(
        console_app, "AUDIT_SECRET", "current-secret-is-32-bytes-long!"
    ), patch.object(console_app, "engine", engine):
        result = await console_app.verify_session_chain(session_id)

    # Must not crash. Must surface as False, not raise.
    assert result["event_count"] == 1
    assert result["events"][0]["verified"] is False
    assert result["verified"] is False
    assert result["first_failure"] == str(pre_event_id)
