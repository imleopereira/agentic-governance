"""F2.5 — kill → halt migration contract.

The v0.6 F2.5 migration (``f251_kill_to_halt_metadata``) is load-bearing
on a single invariant from CLAUDE.md:

    Architectural invariant #2: audit writes MUST be append-only at the
    DB level. UPDATE and DELETE are revoked. The HMAC chain links each
    row by ``prev_hash``, and each row's HMAC covers its ``kind``, so
    rewriting historic ``agent.killed`` rows into ``agent.halted`` would
    break the entire tail of the chain.

The migration therefore creates a SQL view that UNIONs the two kinds
and leaves existing rows untouched. These tests lock in the contract.

Two concerns:

  1. The migration file is syntactically valid, declares the correct
     down_revision, and never calls ``op.execute("UPDATE ...")`` or
     ``op.alter_column(... kind ...)`` against ``governance_audit_events``.
  2. Verifying an HMAC chain that contains BOTH ``agent.killed`` (legacy)
     and ``agent.halted`` (v0.6) rows as siblings still succeeds —
     chain verification only cares about link integrity, not the kind
     string, so mixed-kind chains are a first-class supported state.

A full DB-backed test of the view SELECTing both kinds lives in the
Postgres integration suite (``@pytest.mark.postgres``). This file runs
in the default (in-memory) test tier so the invariant is checked on
every CI run, not just Postgres jobs.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


from codeatelier_governance.audit.chain import compute_event_hmac, verify_event
from codeatelier_governance.audit.models import AuditEventRecord


# v0.6.2 relocated ``migrations/`` into the package (P1 wheel-packaging
# fix) so pip-installed users get the revision files shipped inside the
# wheel. The repo-root copy is gone; tests resolve revisions via the
# src-layout path.
_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "codeatelier_governance"
    / "migrations"
    / "versions"
    / "f251_kill_to_halt_metadata.py"
)


# ---------------------------------------------------------------------------
# 1. Migration file shape
# ---------------------------------------------------------------------------


def test_migration_file_exists() -> None:
    assert _MIGRATION.exists(), f"migration file missing: {_MIGRATION}"


def test_migration_revision_ids() -> None:
    """Locks the revision graph so the merge-head node stays intact."""
    text = _MIGRATION.read_text()
    assert 'revision: str = "f251kill2halt"' in text
    assert 'down_revision: Union[str, Sequence[str], None] = "978884c6b7f1"' in text


def test_migration_creates_view_not_row_rewrite() -> None:
    """The upgrade body must create a VIEW and must NOT rewrite rows.

    A regression here — e.g. a well-meaning ``UPDATE governance_audit_events
    SET kind = 'agent.halted' WHERE kind = 'agent.killed'`` — would break
    the HMAC chain for every row downstream of the first historic kill
    event. This test is the tripwire.
    """
    text = _MIGRATION.read_text()
    assert "CREATE OR REPLACE VIEW {_VIEW_NAME}" in text
    assert '_VIEW_NAME = "governance_audit_events_halted"' in text
    assert "agent.killed" in text
    assert "agent.halted" in text
    # Defensive: no row-mutating SQL against the audit table.
    lowered = text.lower()
    assert "update governance_audit_events" not in lowered
    assert "delete from governance_audit_events" not in lowered
    assert "alter table governance_audit_events" not in lowered


def test_migration_downgrade_drops_view_only() -> None:
    text = _MIGRATION.read_text()
    assert "DROP VIEW IF EXISTS {_VIEW_NAME}" in text


# ---------------------------------------------------------------------------
# 2. HMAC chain with mixed kinds still verifies
# ---------------------------------------------------------------------------


def _record(
    secret: bytes,
    *,
    kind: str,
    prev_hash: str | None,
    session_id,
    agent_id: str = "a",
) -> AuditEventRecord:
    fields: dict = {
        "event_id": uuid4(),
        "session_id": session_id,
        "agent_id": agent_id,
        "parent_event_id": None,
        "kind": kind,
        "input_hash": None,
        "output_hash": None,
        "metadata": {"k": "v"},
        "prev_hash": prev_hash,
        "created_at": datetime.now(timezone.utc),
    }
    mac = compute_event_hmac(secret=secret, **fields)
    return AuditEventRecord(hmac=mac, **fields)


def test_mixed_killed_halted_chain_verifies() -> None:
    """A chain with one legacy ``agent.killed`` row followed by a v0.6
    ``agent.halted`` row still verifies end-to-end. Chain integrity is
    kind-agnostic; this test proves it stays that way."""
    secret = secrets.token_bytes(32)
    sid = uuid4()

    r0 = _record(secret, kind="agent.heartbeat", prev_hash=None, session_id=sid)
    r1 = _record(secret, kind="agent.killed", prev_hash=r0.hmac, session_id=sid)
    r2 = _record(secret, kind="agent.halted", prev_hash=r1.hmac, session_id=sid)
    r3 = _record(secret, kind="agent.heartbeat", prev_hash=r2.hmac, session_id=sid)

    for rec in (r0, r1, r2, r3):
        assert verify_event(rec, secret) is True

    # Linkage
    assert r1.prev_hash == r0.hmac
    assert r2.prev_hash == r1.hmac
    assert r3.prev_hash == r2.hmac


def test_rewriting_legacy_kind_breaks_chain() -> None:
    """If a well-meaning operator rewrote an ``agent.killed`` row to
    ``agent.halted`` in place (without recomputing HMAC), verify_event
    must reject it. This is the exact failure the F2.5 migration avoids."""
    secret = secrets.token_bytes(32)
    sid = uuid4()
    rec = _record(secret, kind="agent.killed", prev_hash=None, session_id=sid)

    # Tamper: swap the kind string without touching the HMAC.
    tampered = AuditEventRecord(
        event_id=rec.event_id,
        session_id=rec.session_id,
        agent_id=rec.agent_id,
        parent_event_id=rec.parent_event_id,
        kind="agent.halted",  # <-- mutated
        input_hash=rec.input_hash,
        output_hash=rec.output_hash,
        metadata=rec.metadata,
        prev_hash=rec.prev_hash,
        created_at=rec.created_at,
        hmac=rec.hmac,
    )
    assert verify_event(tampered, secret) is False
