"""F3 v0.6: response-model wiring tests for the 9 F3 endpoints.

Verifies each targeted endpoint returns a Pydantic-validated instance of the
correct ``StrictResponse`` subclass (not a raw ``dict``). Also unit-tests the
payload redaction module in ``console.redaction`` against every secret shape
listed in the F3 PRD.

The endpoint handlers touch Postgres via SQLAlchemy's ``AsyncEngine``. We
stub ``engine`` with a fake that returns canned rows so these tests stay
pure-unit — no Postgres dependency. The goal is to prove the ``return``
statement constructs the right Pydantic model, not to re-test SQL.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from codeatelier_governance.console import app as app_module
from codeatelier_governance.console.models.responses import (
    AgentPoliciesResponse,
    AgentPresenceResponse,
    BatchApproveResponse,
    EventStatsResponse,
    GateClaimResponse,
    GateContextResponse,
    GateEscalateResponse,
    PolicyListResponse,
    SessionRevokeResponse,
)
from codeatelier_governance.console.redaction import redact_secrets


# ---------------------------------------------------------------------------
# Fake SQLAlchemy engine + connection plumbing
# ---------------------------------------------------------------------------
class _FakeRow(dict):  # type: ignore[type-arg]
    """Dict that also supports positional ``row[0]`` access for SQLAlchemy-ish
    code that treats results as tuples."""

    def __getitem__(self, key: Any) -> Any:  # type: ignore[override]
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _wrap_rows(rows: list[dict[str, Any]] | None) -> list[_FakeRow]:
    return [_FakeRow(r) for r in (rows or [])]


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = _wrap_rows(rows)
        self.rowcount = len(self._rows)

    def mappings(self) -> "_FakeResult":
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeConn:
    def __init__(self, script: list[list[dict[str, Any]] | None]) -> None:
        # ``script`` is a list of row-lists, one per SQL call in order.
        self._script = list(script)

    async def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
        if not self._script:
            return _FakeResult([])
        rows = self._script.pop(0)
        return _FakeResult(rows or [])


class _FakeEngine:
    def __init__(self, script: list[list[dict[str, Any]] | None]) -> None:
        self._script = script

    @asynccontextmanager
    async def connect(self) -> Any:
        yield _FakeConn(list(self._script))

    @asynccontextmanager
    async def begin(self) -> Any:
        yield _FakeConn(list(self._script))


def _install_engine(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
    monkeypatch.setattr(app_module, "engine", _FakeEngine(script))


# ---------------------------------------------------------------------------
# Redaction unit tests
# ---------------------------------------------------------------------------
class TestRedactSecrets:
    def test_anthropic_key(self) -> None:
        out = redact_secrets("here's my sk-ant-api03-abcdefghijklmnopqrstuv")
        assert "sk-ant-" not in out
        assert "[REDACTED]" in out

    def test_openai_key(self) -> None:
        out = redact_secrets("sk-proj-abcdefghijklmnopqrstuvwx1234")
        assert "[REDACTED]" in out
        assert "sk-proj-" not in out

    def test_slack_bot_token(self) -> None:
        out = redact_secrets("xoxb-1234567890-abcdefghijklmnop")
        assert "[REDACTED]" in out

    def test_github_pat(self) -> None:
        out = redact_secrets("ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        assert "[REDACTED]" in out
        out2 = redact_secrets("ghs_abcdefghijklmnopqrstuvwxyz0123456789")
        assert "[REDACTED]" in out2

    def test_aws_access_key_id(self) -> None:
        out = redact_secrets("AKIAIOSFODNN7EXAMPLE")
        assert out == "[REDACTED]"

    def test_aws_secret_access_key_keyed(self) -> None:
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1"
        out = redact_secrets(f'aws_secret_access_key="{secret}"')
        assert "[REDACTED]" in out
        assert secret not in out

    def test_recursive_dict(self) -> None:
        out = redact_secrets(
            {
                "note": "my key is sk-ant-abcdefghijklmnopqrstuv",
                "safe": "hello",
                "nested": {"inner": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
                "list": ["AKIAIOSFODNN7EXAMPLE", 42, None, True],
            }
        )
        assert "[REDACTED]" in out["note"]
        assert out["safe"] == "hello"
        assert out["nested"]["inner"] == "[REDACTED]"
        assert out["list"][0] == "[REDACTED]"
        assert out["list"][1] == 42
        assert out["list"][2] is None
        assert out["list"][3] is True

    def test_all_six_patterns_in_one_blob(self) -> None:
        blob = (
            "sk-ant-api03-abcdefghijklmnopqrstuv "
            "sk-proj-abcdefghijklmnopqrstuvwx1234 "
            "xoxb-1234567890-abcdefghijklmnop "
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
            "AKIAIOSFODNN7EXAMPLE "
            'aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY1"'
        )
        out = redact_secrets(blob)
        # Every raw secret prefix should be gone.
        for marker in (
            "sk-ant-api03",
            "sk-proj-",
            "xoxb-1234567890",
            "ghp_abcdef",
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI",
        ):
            assert marker not in out, f"{marker} leaked through redaction"
        assert out.count("[REDACTED]") >= 6


# ---------------------------------------------------------------------------
# Per-endpoint tests: each must return the right Pydantic model type.
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_list_policies_returns_policy_list_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_engine(
        monkeypatch,
        [
            [
                {
                    "agent_id": "a1",
                    "policy_type": "scope",
                    "policy_json": {"tools": "web_search", "note": "sk-ant-abcdefghijklmnopqrstuv"},
                    "updated_at": _NOW,
                }
            ]
        ],
    )
    result = await app_module.list_policies()
    assert isinstance(result, PolicyListResponse)
    assert len(result.policies) == 1
    # Redaction applied to nested policy values.
    assert "[REDACTED]" in result.policies[0].policy["note"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_get_agent_policies_returns_agent_policies_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_engine(
        monkeypatch,
        [
            [
                {
                    "agent_id": "a1",
                    "policy_type": "budget",
                    "policy_json": {"usd_per_day": 10.0},
                    "updated_at": _NOW,
                }
            ]
        ],
    )
    result = await app_module.get_agent_policies("a1")
    assert isinstance(result, AgentPoliciesResponse)
    assert result.agent_id == "a1"
    assert result.policies[0].policy_type == "budget"


@pytest.mark.asyncio
async def test_agent_presence_returns_agent_presence_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_engine(
        monkeypatch,
        [
            [
                {
                    "agent_id": "a1",
                    "status": "active",
                    "last_heartbeat": _NOW,
                    "started_at": _NOW,
                    "metadata_json": {"hostname": "h1"},
                }
            ]
        ],
    )
    result = await app_module.agent_presence()
    assert isinstance(result, AgentPresenceResponse)
    assert result.agents[0].agent_id == "a1"


@pytest.mark.asyncio
async def test_event_stats_returns_event_stats_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # event_stats executes: SET LOCAL statement_timeout (1),
    # per-kind SELECT (2), 5-min total SELECT (3)
    _install_engine(
        monkeypatch,
        [
            None,  # SET LOCAL
            [{"kind": "llm.call", "cnt": 10}, {"kind": "tool.call", "cnt": 5}],
            [{"cnt": 2}],
        ],
    )
    # _FakeResult.first() on the 5-min row needs integer indexing support.
    # Patch _FakeResult to support [0] via row tuples by using a custom shim.
    result = await app_module.event_stats()
    assert isinstance(result, EventStatsResponse)
    assert result.total_last_hour == 15


@pytest.mark.asyncio
async def test_gate_context_returns_gate_context_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = uuid4()
    _install_engine(
        monkeypatch,
        [
            # gate row
            [
                {
                    "request_id": rid,
                    "agent_id": "a1",
                    "kind": "tool.call",
                    "action_hash": "hash",
                    "created_at": _NOW,
                    "expires_at": _NOW,
                    "resolved_at": None,
                    "resolution": None,
                    "payload_json": {"risk": "LOW", "note": "sk-ant-abcdefghijklmnopqrstuv"},
                    "reviewer_id": None,
                    "reviewing_since": None,
                    "rationale": None,
                }
            ],
            # presence row
            [{"agent_id": "a1", "status": "active", "last_heartbeat": _NOW}],
            # recent events
            [{"kind": "llm.call", "created_at": _NOW}],
            # cost row
            [{"usd_used": 1.25}],
        ],
    )
    result = await app_module.gate_context(rid)
    assert isinstance(result, GateContextResponse)
    assert result.risk == "LOW"
    assert "[REDACTED]" in str(result.payload)


@pytest.mark.asyncio
async def test_claim_gate_returns_gate_claim_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = uuid4()
    reviewer = uuid4()
    _install_engine(
        monkeypatch,
        [
            [{"resolved_at": None, "reviewer_id": None}],
            [{"request_id": rid}],
        ],
    )
    request = MagicMock()
    request.state.user_id = str(reviewer)
    result = await app_module.claim_gate(rid, request)
    assert isinstance(result, GateClaimResponse)
    assert result.ok is True


@pytest.mark.asyncio
async def test_escalate_gate_returns_gate_escalate_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = uuid4()
    _install_engine(
        monkeypatch,
        [
            [
                {
                    "request_id": rid,
                    "resolved_at": None,
                    "payload_json": {"risk": "LOW"},
                }
            ],
            None,  # UPDATE
        ],
    )
    request = MagicMock()
    request.state.user_id = "opid"
    body = app_module.EscalateRequest(escalate_to="role:senior")
    result = await app_module.escalate_gate(rid, body, request)
    assert isinstance(result, GateEscalateResponse)
    assert result.escalated_to == "role:senior"


@pytest.mark.asyncio
async def test_batch_approve_returns_batch_approve_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = uuid4()
    # batch_approve calls _check_self_approval which does its own SELECT;
    # stub it to a no-op so we don't need to hand-roll the script for it.
    async def _no_self_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(app_module, "_check_self_approval", _no_self_check)
    monkeypatch.setattr(app_module, "AUDIT_SECRET", "x" * 32)
    monkeypatch.setattr(app_module, "audit_module", None)
    _install_engine(
        monkeypatch,
        [
            [
                {
                    "request_id": rid,
                    "agent_id": "a1",
                    "kind": "tool.call",
                    "payload_json": {"risk": "LOW"},
                    "resolved_at": None,
                    "reviewer_id": None,
                }
            ],
            None,  # UPDATE
        ],
    )
    request = MagicMock()
    request.state.user_id = str(uuid4())
    body = app_module.BatchApproveRequest(request_ids=[rid])
    result = await app_module.batch_approve(body, request)
    assert isinstance(result, BatchApproveResponse)
    assert result.ok is True
    assert len(result.approved) + len(result.failed) == 1


@pytest.mark.asyncio
async def test_revoke_session_returns_session_revoke_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid4()
    _install_engine(
        monkeypatch,
        [
            [{"created_at": _NOW}],  # UPDATE ... RETURNING
        ],
    )
    monkeypatch.setattr(app_module, "audit_module", None)
    monkeypatch.setattr(app_module, "_revoke_sse_session", lambda _sid: None)
    request = MagicMock()
    request.state.user_id = "admin-uid"
    result = await app_module.revoke_session(sid, request)
    assert isinstance(result, SessionRevokeResponse)
    assert result.ok is True
    assert result.session_id == str(sid)


@pytest.mark.asyncio
async def test_revoke_session_emits_pipeline_session_revoked_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid4()
    _install_engine(
        monkeypatch,
        [
            [{"created_at": _NOW}],
        ],
    )
    logged: list[Any] = []

    class _FakeAudit:
        async def log(self, event: Any) -> None:
            logged.append(event)

    monkeypatch.setattr(app_module, "audit_module", _FakeAudit())
    monkeypatch.setattr(app_module, "_revoke_sse_session", lambda _sid: None)
    request = MagicMock()
    request.state.user_id = "operator-42"
    await app_module.revoke_session(sid, request)
    assert len(logged) == 1
    assert logged[0].kind == "pipeline.session_revoked"
    assert logged[0].metadata["session_id"] == str(sid)
    # Operator id is pseudonymized, not leaked raw.
    assert logged[0].metadata["revoked_by"] != "operator-42"
    assert len(logged[0].metadata["revoked_by"]) == 16


# ---------- DA Wave 4 BLOCKER 2: typed scope list fields on PolicyRow ----

def test_policy_row_accepts_allowed_tools_list() -> None:
    """PolicyRow must accept list[str] on the typed top-level attributes."""
    from codeatelier_governance.console.models.responses import PolicyRow

    row = PolicyRow(
        agent_id="a",
        policy_type="scope",
        policy={},
        allowed_tools=["tool_a", "tool_b"],
        hidden_tools=None,
        allowed_apis=["https://example.com"],
        allowed_models=["gpt-4"],
    )
    dumped = row.model_dump()
    assert dumped["allowed_tools"] == ["tool_a", "tool_b"]
    assert dumped["allowed_apis"] == ["https://example.com"]
    assert dumped["allowed_models"] == ["gpt-4"]


def test_policy_row_rejects_list_in_policy_dict() -> None:
    """The `policy` dict MUST remain scalar-only (MetadataValue)."""
    from pydantic import ValidationError

    from codeatelier_governance.console.models.responses import PolicyRow

    with pytest.raises(ValidationError):
        PolicyRow(
            agent_id="a",
            policy_type="scope",
            policy={"bad": ["shouldnt_be_here"]},  # type: ignore[dict-item]
        )
