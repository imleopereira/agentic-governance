"""v0.6.2 followup ship-blocker: /api/gates/pending needs a role gate.

Pre-followup, ``GET /api/gates/pending`` had only
``Depends(authenticate), Depends(rate_limit_per_user)`` — no role check,
no per-agent scope. Any authenticated user (including read-only
viewers) could enumerate the full cross-agent approval queue. That's
an exfiltration surface: viewer accounts leak the pending-action
fingerprint of every agent in the tenant.

Interim fix for v0.6.2 (no per-agent scope schema in the auth module
yet): require ``admin`` role. Compensating control — grant/deny were
already admin-only at ``app.py:914/947``, so restricting read to admin
doesn't break any real workflow. v0.6.3 adds a ``reviewer`` role plus
``reviewer_agent_scope`` table so scoped reviewers can see only their
agents' queue.

These tests drive ``require_role("admin")`` in isolation (same pattern
as ``test_endpoints.py``) because the HTTP layer needs a full FastAPI
TestClient + session cookie, which is overkill for a role check.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def _build_request(role: str | None) -> MagicMock:
    request = MagicMock()
    request.state.role = role
    request.state.user_id = "test-user-1"
    return request


def test_viewer_denied_on_pending_queue() -> None:
    """A ``viewer`` session gets 403 when hitting /api/gates/pending.

    This is the ship-blocker lock: prior to this fix, a viewer would
    get 200 + the full queue. The require_role("admin") dependency
    must fire before the handler body.
    """
    from codeatelier_governance.console.app import require_role

    dep = require_role("admin")
    check_fn = dep.dependency

    request = _build_request("viewer")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(check_fn(request))
    assert exc_info.value.status_code == 403


def test_admin_allowed_on_pending_queue() -> None:
    """An ``admin`` session passes the role gate."""
    from codeatelier_governance.console.app import require_role

    dep = require_role("admin")
    check_fn = dep.dependency

    request = _build_request("admin")
    # Should NOT raise.
    asyncio.run(check_fn(request))


def test_missing_role_denied() -> None:
    """No role attribute at all (e.g. auth bypass bug) → 403, not 200."""
    from codeatelier_governance.console.app import require_role

    dep = require_role("admin")
    check_fn = dep.dependency

    request = MagicMock()
    request.state.role = None
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(check_fn(request))
    assert exc_info.value.status_code == 403


def test_pending_route_has_admin_dependency() -> None:
    """Structural: the route wiring includes require_role("admin").

    If someone refactors and drops the dependency, this test catches
    the regression without needing a live DB. We introspect the
    registered FastAPI route, not the handler function directly.
    """
    from codeatelier_governance.console import app as console_app

    v1_route = next(
        r
        for r in console_app.app.routes
        if getattr(r, "path", "") == "/api/gates/pending"
    )
    v2_route = next(
        r
        for r in console_app.app.routes
        if getattr(r, "path", "") == "/api/v2/gates/pending"
    )

    # Each dependency is a Depends(...) object. Stringify the wrapped
    # callable and assert at least one is require_role's check closure.
    def _has_admin_role(route: object) -> bool:
        deps = getattr(route, "dependant", None)
        if deps is None:
            # Fallback: inspect the route's dependencies attribute.
            raw_deps = getattr(route, "dependencies", []) or []
            for d in raw_deps:
                fn = getattr(d, "dependency", None)
                if fn is not None and getattr(fn, "__qualname__", "").startswith("require_role"):
                    return True
            return False
        # FastAPI normalizes into dependant.dependencies.
        for sub in deps.dependencies:
            fn = getattr(sub, "call", None)
            if fn is not None and getattr(fn, "__qualname__", "").startswith("require_role"):
                return True
        return False

    assert _has_admin_role(v1_route), (
        "/api/gates/pending is missing require_role('admin') — "
        "ship-blocker regression"
    )
    assert _has_admin_role(v2_route), (
        "/api/v2/gates/pending is missing require_role('admin') — "
        "ship-blocker regression on v2 route"
    )
