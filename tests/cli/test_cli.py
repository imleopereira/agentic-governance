"""Tests for the governance CLI.

Tests use in-process calls to the CLI entry point. Integration tests against
Postgres are marked with @pytest.mark.integration.
"""
from __future__ import annotations

import json
import os
import secrets
from uuid import uuid4

import pytest

from codeatelier_governance.cli.commands import (
    _build_parser,
    _run_budget,
    _run_migrate,
    _run_verify,
    main,
)


# -- Parser tests --------------------------------------------------------------


def test_parser_migrate_command() -> None:
    parser = _build_parser()
    args = parser.parse_args(["migrate", "--database-url", "postgresql://localhost/test"])
    assert args.command == "migrate"
    assert args.database_url == "postgresql://localhost/test"


def test_parser_verify_command() -> None:
    parser = _build_parser()
    sid = str(uuid4())
    args = parser.parse_args(["verify", "--database-url", "postgresql://x", "--session-id", sid])
    assert args.command == "verify"
    assert args.session_id == sid


def test_parser_tail_command() -> None:
    parser = _build_parser()
    args = parser.parse_args([
        "tail", "--database-url", "postgresql://x",
        "--agent-id", "a1", "--kind", "llm.call",
    ])
    assert args.command == "tail"
    assert args.agent_id == "a1"
    assert args.kind == "llm.call"


def test_parser_budget_command() -> None:
    parser = _build_parser()
    args = parser.parse_args(["budget", "--database-url", "postgresql://x", "--agent-id", "a1"])
    assert args.command == "budget"
    assert args.agent_id == "a1"


# -- Database URL resolution ---------------------------------------------------


def test_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """--database-url can be omitted if GOVERNANCE_DATABASE_URL is set."""
    monkeypatch.setenv("GOVERNANCE_DATABASE_URL", "postgresql://from-env/db")
    parser = _build_parser()
    args = parser.parse_args(["migrate"])
    from codeatelier_governance.cli.commands import _resolve_database_url

    url = _resolve_database_url(args)
    assert url == "postgresql://from-env/db"


def test_database_url_missing_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing --database-url and no env var should exit with code 2."""
    monkeypatch.delenv("GOVERNANCE_DATABASE_URL", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["migrate"])
    from codeatelier_governance.cli.commands import _resolve_database_url

    with pytest.raises(SystemExit) as exc_info:
        _resolve_database_url(args)
    assert exc_info.value.code == 2


# -- Migrate idempotency (integration) ----------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migrate_idempotent() -> None:
    """Running migrate twice should succeed (IF NOT EXISTS in DDL)."""
    url = os.environ.get("GOVERNANCE_DATABASE_URL", "postgresql://postgres:postgres@localhost:5435/postgres")
    await _run_migrate(url)
    await _run_migrate(url)  # Should not raise


# -- Verify exit codes (integration) -------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_clean_session() -> None:
    """Verify should exit 0 for a valid HMAC chain."""
    url = os.environ.get("GOVERNANCE_DATABASE_URL", "postgresql://postgres:postgres@localhost:5435/postgres")
    secret_hex = secrets.token_hex(32)

    # First, migrate
    await _run_migrate(url)

    # Insert a valid chain via the SDK
    os.environ["GOVERNANCE_AUDIT_SECRET"] = secret_hex

    from codeatelier_governance import GovernanceSDK

    sdk = GovernanceSDK(database_url=url, audit_secret=secret_hex.encode())
    await sdk.start()

    from codeatelier_governance.audit.models import AuditEvent

    with sdk.audit.session() as sid:
        await sdk.audit.log(AuditEvent(agent_id="cli-test", kind="test.one"))
        await sdk.audit.log(AuditEvent(agent_id="cli-test", kind="test.two"))

    await sdk.close()

    exit_code = await _run_verify(url, sid)
    assert exit_code == 0

    # Clean up
    del os.environ["GOVERNANCE_AUDIT_SECRET"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verify_empty_session() -> None:
    """Verify should exit 1 for a session with no events."""
    url = os.environ.get("GOVERNANCE_DATABASE_URL", "postgresql://postgres:postgres@localhost:5435/postgres")
    secret_hex = secrets.token_hex(32)
    os.environ["GOVERNANCE_AUDIT_SECRET"] = secret_hex

    await _run_migrate(url)
    exit_code = await _run_verify(url, uuid4())
    assert exit_code == 1

    del os.environ["GOVERNANCE_AUDIT_SECRET"]


# -- Budget display (integration) ----------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_shows_data(capsys: pytest.CaptureFixture[str]) -> None:
    """Budget command should output JSON with agent_id."""
    url = os.environ.get("GOVERNANCE_DATABASE_URL", "postgresql://postgres:postgres@localhost:5435/postgres")
    await _run_migrate(url)
    await _run_budget(url, "nonexistent-agent")

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["agent_id"] == "nonexistent-agent"
    assert "sessions" in data
    assert "daily" in data


# -- Main entry point ----------------------------------------------------------


def test_main_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Running with no args should print help and exit 0."""
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 0
