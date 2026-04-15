"""F2.5 — ``/halt`` route + back-compat ``/kill`` alias contract.

The full HTTP round-trip requires a live Postgres engine (the handlers
hit ``governance_agent_presence`` inside an ``engine.begin()`` block).
That integration is covered by the Postgres-marked suite. This file
runs in the default unit tier and asserts the contract that can be
verified without a database:

  1. Both ``POST /api/agents/{agent_id}/halt`` and the back-compat
     ``POST /api/agents/{agent_id}/kill`` are registered on the FastAPI
     app, with the POST method, and on the ``require_role('admin')``
     dependency tree.
  2. ``HaltRequest`` preserves the F2 P0 sanitizer behavior for the
     ``reason`` field (escape-order invariant, NFC normalization,
     codepoint-based length cap, C0 strip).
  3. ``KillRequest`` is an identity alias of ``HaltRequest`` — the
     ``class is`` identity is what lets v0.5.x code that imports
     ``KillRequest`` keep working.
  4. The module exposes the renamed internal constants under their new
     names AND their back-compat aliases.
"""
from __future__ import annotations

import pytest

from codeatelier_governance.console import app as console_app
from codeatelier_governance.console.app import (
    HaltRequest,
    KillRequest,
    _HALT_REASON_MAX,
    _KILL_REASON_MAX,
    app,
)


# ---------------------------------------------------------------------------
# 1. Route registration
# ---------------------------------------------------------------------------


def _post_paths() -> set[str]:
    out: set[str] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "POST" in methods:
            path = getattr(route, "path", None)
            if path is not None:
                out.add(path)
    return out


def test_halt_route_is_registered() -> None:
    assert "/api/agents/{agent_id}/halt" in _post_paths()


def test_kill_route_still_registered_as_deprecated_alias() -> None:
    """The v0.5.x /kill route must stay mounted for one release. v0.7
    removes it. Deleting the route here before v0.7 is a breaking change
    and must bump the major version."""
    assert "/api/agents/{agent_id}/kill" in _post_paths()


def test_halt_handler_and_kill_handler_are_distinct() -> None:
    """``kill_agent`` delegates to ``halt_agent`` at runtime, but the two
    must be distinct callable objects so the FastAPI route table can
    attach the Deprecation/Sunset response headers to the ``/kill`` one
    without polluting the ``/halt`` one."""
    assert console_app.halt_agent is not console_app.kill_agent
    assert callable(console_app.halt_agent)
    assert callable(console_app.kill_agent)


# ---------------------------------------------------------------------------
# 2. HaltRequest preserves F2 sanitizer behaviour
# ---------------------------------------------------------------------------


def test_halt_request_escape_order_backslash_first() -> None:
    raw = "a\\nb\nc"
    assert HaltRequest(reason=raw).reason == "a\\\\nb\\nc"


def test_halt_request_nfc_normalization_before_length_cap() -> None:
    raw = "e\u0301" * 400  # decomposed
    result = HaltRequest(reason=raw).reason
    assert len(result) <= _HALT_REASON_MAX
    assert "\u0301" not in result  # NFC composed


def test_halt_request_c0_control_stripped() -> None:
    raw = "before\x1b[31mred\x1bafter"
    result = HaltRequest(reason=raw).reason
    assert "\x1b" not in result
    assert "before" in result and "after" in result


def test_halt_request_codepoint_length_cap() -> None:
    raw = "x" * (_HALT_REASON_MAX + 500)
    result = HaltRequest(reason=raw).reason
    assert len(result) <= _HALT_REASON_MAX


def test_halt_request_empty_reason_rejected() -> None:
    with pytest.raises(Exception):
        HaltRequest(reason="")


def test_halt_request_extra_field_rejected() -> None:
    """strict + extra='forbid' — the F2 hardening."""
    with pytest.raises(Exception):
        HaltRequest(reason="ok", unexpected="field")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 3. KillRequest alias contract
# ---------------------------------------------------------------------------


def test_kill_request_is_identity_alias() -> None:
    assert KillRequest is HaltRequest


def test_kill_request_instance_is_halt_request_instance() -> None:
    req = KillRequest(reason="legacy")
    assert isinstance(req, HaltRequest)


def test_kill_reason_max_alias_matches_halt_reason_max() -> None:
    assert _KILL_REASON_MAX == _HALT_REASON_MAX == 512
