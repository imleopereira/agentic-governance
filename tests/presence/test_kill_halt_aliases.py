"""F2.5 — back-compat alias contract.

v0.6 renames the "kill" vocabulary to "halt" across the SDK and console
(see ``decisions/2026-04-14-v06-major-release-prd.md`` F2.5). This file
locks in the alias guarantees that are promised for the one-release
deprecation window (v0.6 ships both names; v0.7 deletes the old).

Concretely:

  * ``AgentKilledError`` is the SAME class object as ``AgentHaltedError``
    (identity alias, not a subclass). ``isinstance(err, AgentKilledError)``
    and ``isinstance(err, AgentHaltedError)`` are both True for any
    instance raised by the presence module.
  * ``presence.is_killed(agent_id)`` returns the same value as
    ``presence.is_halted(agent_id)``.
  * ``presence.assert_alive(agent_id)`` behaves like
    ``presence.assert_not_halted(agent_id)`` (same raise / no-raise).
  * ``presence._killed_cache`` proxies to ``presence._halted_cache`` and
    reads back in the v0.5.x shape (``killed_by`` / ``killed_at`` keys).
  * ``presence._killed_cache_at`` proxies to ``presence._halted_cache_at``.
  * ``KillRequest`` in ``codeatelier_governance.console.app`` is the same
    class as ``HaltRequest``.

These tests exist so a refactor in v0.6.x cannot silently drop the
aliases before v0.7. Deleting any assertion here should force a version
bump + a CHANGELOG entry.
"""
from __future__ import annotations

import pytest

from codeatelier_governance.presence import (
    AgentHaltedError,
    AgentKilledError,
    PresenceModule,
)
from codeatelier_governance.presence import errors as presence_errors


# ---------------------------------------------------------------------------
# Exception alias
# ---------------------------------------------------------------------------


def test_agent_killed_error_is_identity_alias_of_halted() -> None:
    """The two names refer to the exact same class object."""
    assert AgentKilledError is AgentHaltedError
    assert presence_errors.AgentKilledError is presence_errors.AgentHaltedError


def test_raising_halted_is_caught_as_killed() -> None:
    """Existing v0.5.x `except AgentKilledError:` clauses catch the new one."""
    with pytest.raises(AgentKilledError):
        raise AgentHaltedError("agent-1", halted_by="op", reason="r")


def test_raising_killed_is_caught_as_halted() -> None:
    """New v0.6 `except AgentHaltedError:` clauses catch the legacy name."""
    with pytest.raises(AgentHaltedError):
        raise AgentKilledError("agent-1", killed_by="op", reason="r")


def test_legacy_killed_kwargs_populate_halted_attributes() -> None:
    """v0.5.x callers constructing the error with `killed_by=` / `killed_at=`
    must still populate the attributes. The read-side is via `halted_by` /
    `halted_at` or via the `killed_by` / `killed_at` @property aliases."""
    err = AgentKilledError(
        "agent-1",
        killed_by="leo",
        killed_at="2026-04-14T00:00:00+00:00",
        reason="legacy",
    )
    assert err.halted_by == "leo"
    assert err.halted_at == "2026-04-14T00:00:00+00:00"
    assert err.killed_by == "leo"
    assert err.killed_at == "2026-04-14T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Method aliases on PresenceModule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_killed_matches_is_halted(presence: PresenceModule) -> None:
    await presence.heartbeat("agent-1")
    assert (
        await presence.is_killed("agent-1")
        == await presence.is_halted("agent-1")
    )

    async with presence._lock:
        presence._agents["agent-1"]["halted_by"] = "op"
    await presence.force_refresh_halted_cache()
    assert await presence.is_killed("agent-1") is True
    assert await presence.is_halted("agent-1") is True


@pytest.mark.asyncio
async def test_assert_alive_matches_assert_not_halted(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    await presence.assert_alive("agent-1")  # live → no raise
    await presence.assert_not_halted("agent-1")  # live → no raise

    async with presence._lock:
        presence._agents["agent-1"]["halted_by"] = "op"
    await presence.force_refresh_halted_cache()
    with pytest.raises(AgentHaltedError):
        await presence.assert_alive("agent-1")
    with pytest.raises(AgentHaltedError):
        await presence.assert_not_halted("agent-1")


@pytest.mark.asyncio
async def test_killed_cache_property_proxies_halted_cache(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    async with presence._lock:
        presence._agents["agent-1"]["halted_by"] = "op"
    await presence.force_refresh_halted_cache()

    # Read side: legacy name returns v0.5.x-shaped entries.
    legacy = presence._killed_cache
    assert "agent-1" in legacy
    assert legacy["agent-1"]["killed_by"] == "op"

    # Write side: assigning to `_killed_cache` translates into the new store.
    presence._killed_cache = {
        "agent-2": {"killed_by": "op2", "killed_at": None, "reason": None}
    }
    assert "agent-2" in presence._halted_cache
    assert presence._halted_cache["agent-2"]["halted_by"] == "op2"


@pytest.mark.asyncio
async def test_killed_cache_at_proxies(presence: PresenceModule) -> None:
    presence._killed_cache_at = 123.0
    assert presence._halted_cache_at == 123.0
    presence._halted_cache_at = 456.0
    assert presence._killed_cache_at == 456.0


@pytest.mark.asyncio
async def test_force_refresh_killed_cache_still_works(
    presence: PresenceModule,
) -> None:
    await presence.heartbeat("agent-1")
    await presence.force_refresh_killed_cache()  # legacy name
    assert await presence.is_halted("agent-1") is False
