"""Tests for the F9 ``/api/coverage`` console endpoint."""
from __future__ import annotations

import pytest

from codeatelier_governance.console.models.responses import (
    WrapperCoverageAgentEntry,
    WrapperCoverageView,
)


def test_wrapper_coverage_view_is_strict_forbid() -> None:
    """Response model must inherit StrictResponse with extra=forbid."""
    assert WrapperCoverageView.model_config.get("extra") == "forbid"
    assert WrapperCoverageView.model_config.get("strict") is True


def test_wrapper_coverage_view_rejects_unknown_fields() -> None:
    from datetime import datetime, timezone

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WrapperCoverageView(
            as_of=datetime.now(timezone.utc),
            active_wrappers=0,
            total_wrappers=0,
            coverage_pct=None,
            coverage_pct_reason="no_scope_policies_registered",
            by_agent=[],
            unwrapped_agents_seen_in_audit=[],
            active_window_days=7,
            sneaky="leak",  # type: ignore[call-arg]
        )


def test_wrapper_coverage_view_no_scope_policies_case() -> None:
    from datetime import datetime, timezone

    view = WrapperCoverageView(
        as_of=datetime.now(timezone.utc),
        active_wrappers=0,
        total_wrappers=0,
        coverage_pct=None,
        coverage_pct_reason="no_scope_policies_registered",
        by_agent=[],
        unwrapped_agents_seen_in_audit=[],
        active_window_days=7,
    )
    assert view.coverage_pct is None
    assert view.coverage_pct_reason == "no_scope_policies_registered"


def test_wrapper_coverage_view_ok_case() -> None:
    from datetime import datetime, timezone

    view = WrapperCoverageView(
        as_of=datetime.now(timezone.utc),
        active_wrappers=3,
        total_wrappers=4,
        coverage_pct=0.75,
        coverage_pct_reason="ok",
        by_agent=[
            WrapperCoverageAgentEntry(
                agent_id="a1",
                provider="openai",
                active=True,
                last_seen_at=datetime.now(timezone.utc),
            ),
        ],
        unwrapped_agents_seen_in_audit=["legacy-cron"],
        active_window_days=7,
    )
    assert view.coverage_pct == 0.75
    assert view.coverage_pct_reason == "ok"
    assert view.by_agent[0].provider == "openai"
    assert view.unwrapped_agents_seen_in_audit == ["legacy-cron"]


def test_endpoint_registered() -> None:
    """The route must be wired into the FastAPI app."""
    from codeatelier_governance.console.app import app

    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/api/coverage" in paths


def test_endpoint_returns_503_when_engine_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB unreachable → 503."""
    import asyncio

    from fastapi import HTTPException

    from codeatelier_governance.console import app as app_module

    monkeypatch.setattr(app_module, "engine", None)

    async def _run() -> None:
        with pytest.raises(HTTPException) as excinfo:
            await app_module.wrapper_coverage(agent_id=None, active_window_days=7)
        assert excinfo.value.status_code == 503

    asyncio.run(_run())
