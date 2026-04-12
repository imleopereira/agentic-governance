"""Happy-path + exploit tests for the scope module."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from codeatelier_governance.audit import AuditModule, InMemoryAuditStore
from codeatelier_governance.scope import (
    PolicyNotRegistered,
    ScopeModule,
    ScopePolicy,
    ScopeViolation,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_allowed_tool_passes(scope: ScopeModule) -> None:
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_tools=frozenset({"read_invoice", "send_email"}),
        )
    )
    await scope.check(agent_id="a", tool="read_invoice")  # no raise


@pytest.mark.asyncio
async def test_disallowed_tool_raises_and_logs(
    scope: ScopeModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    scope.register(
        ScopePolicy(agent_id="a", allowed_tools=frozenset({"read_invoice"}))
    )
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="a", tool="delete_customer")
    await audit._writer.flush()
    events = list(audit_store._events.values())  # type: ignore[attr-defined]
    violations = [e for e in events if e.kind == "scope.violation"]
    assert len(violations) == 1
    assert violations[0].metadata["tool"] == "delete_customer"
    assert violations[0].metadata["reason"] == "tool not whitelisted"


@pytest.mark.asyncio
async def test_unknown_agent_default_denies(
    scope: ScopeModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    with pytest.raises(PolicyNotRegistered):
        await scope.check(agent_id="ghost", tool="anything")
    await audit._writer.flush()
    events = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "scope.violation"
    ]
    assert events[0].metadata["reason"] == "no policy registered"


@pytest.mark.asyncio
async def test_api_exact_match(scope: ScopeModule) -> None:
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_apis=frozenset({"POST https://api.stripe.com/v1/charges"}),
        )
    )
    await scope.check(agent_id="a", api="POST https://api.stripe.com/v1/charges")


@pytest.mark.asyncio
async def test_api_prefix_match(scope: ScopeModule) -> None:
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_apis=frozenset({"GET https://api.stripe.com/v1/customers/*"}),
        )
    )
    await scope.check(
        agent_id="a", api="GET https://api.stripe.com/v1/customers/cus_123"
    )
    with pytest.raises(ScopeViolation):
        await scope.check(
            agent_id="a", api="DELETE https://api.stripe.com/v1/customers/cus_123"
        )


@pytest.mark.asyncio
async def test_decorator_blocks_disallowed(scope: ScopeModule) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"read"})))

    @scope.require_tool("delete", agent_id="a")
    async def dangerous() -> str:
        return "should not run"

    with pytest.raises(ScopeViolation):
        await dangerous()


@pytest.mark.asyncio
async def test_decorator_allows_whitelisted(scope: ScopeModule) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"read"})))

    @scope.require_tool("read", agent_id="a")
    async def safe() -> str:
        return "ok"

    assert await safe() == "ok"


@pytest.mark.asyncio
async def test_check_requires_tool_or_api(scope: ScopeModule) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"x"})))
    with pytest.raises(ValueError, match="tool=... or api=..."):
        await scope.check(agent_id="a")


# ---------------------------------------------------------------------------
# Exploit / cybersecurity tests
# ---------------------------------------------------------------------------
def test_policy_is_frozen() -> None:
    """An attacker who gets a reference to a policy cannot mutate it."""
    p = ScopePolicy(agent_id="a", allowed_tools=frozenset({"read"}))
    with pytest.raises(ValidationError):
        p.agent_id = "evil"  # type: ignore[misc]


def test_policy_rejects_oversized_tool_set() -> None:
    huge = frozenset({f"tool_{i}" for i in range(2000)})
    with pytest.raises(ValueError):
        ScopePolicy(agent_id="a", allowed_tools=huge)


def test_policy_rejects_empty_tool_name() -> None:
    with pytest.raises(ValueError):
        ScopePolicy(agent_id="a", allowed_tools=frozenset({""}))


@pytest.mark.asyncio
async def test_regex_chars_in_tool_name_are_literal(scope: ScopeModule) -> None:
    """A whitelist entry like 'read.*' must NOT match 'read_anything'."""
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"read.*"})))
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="a", tool="read_invoice")
    # exact 'read.*' is allowed (it's a valid string in the whitelist)
    await scope.check(agent_id="a", tool="read.*")


@pytest.mark.asyncio
async def test_glob_chars_in_api_are_literal_unless_explicit_prefix(
    scope: ScopeModule,
) -> None:
    scope.register(
        ScopePolicy(agent_id="a", allowed_apis=frozenset({"POST https://api.x.com/*"}))
    )
    # Prefix match: anything starting with the prefix
    await scope.check(agent_id="a", api="POST https://api.x.com/anything")
    # But the literal "POST https://api.evil.com/*" must NOT match
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="a", api="POST https://api.evil.com/x")


@pytest.mark.asyncio
async def test_decorator_rejects_sync_function(scope: ScopeModule) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"x"})))
    with pytest.raises(TypeError, match="async function"):

        @scope.require_tool("x", agent_id="a")
        def sync_fn() -> None:
            pass


@pytest.mark.asyncio
async def test_concurrent_checks_all_log_independently(
    scope: ScopeModule, audit_store: InMemoryAuditStore, audit: AuditModule
) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"x"})))
    # 20 concurrent denied calls
    results = await asyncio.gather(
        *(
            asyncio.create_task(_attempt_violation(scope))
            for _ in range(20)
        )
    )
    assert all(isinstance(r, ScopeViolation) for r in results)
    await audit._writer.flush()
    violations = [
        e
        for e in audit_store._events.values()  # type: ignore[attr-defined]
        if e.kind == "scope.violation"
    ]
    assert len(violations) == 20


async def _attempt_violation(scope: ScopeModule) -> Exception:
    try:
        await scope.check(agent_id="a", tool="forbidden")
    except ScopeViolation as exc:
        return exc
    return RuntimeError("expected violation")


# ---------------------------------------------------------------------------
# G10: Hidden tool policies
# ---------------------------------------------------------------------------
def test_filter_tools_removes_hidden(scope: ScopeModule) -> None:
    """filter_tools should remove tools in the hidden_tools set."""
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_tools=frozenset({"read", "write"}),
            hidden_tools=frozenset({"secret_tool", "internal_api"}),
        )
    )
    tools = ["read", "write", "secret_tool", "internal_api", "other"]
    result = scope.filter_tools("a", tools)
    assert result == ["read", "write", "other"]


def test_filter_tools_no_hidden_returns_full_list(scope: ScopeModule) -> None:
    """Policy with no hidden_tools returns the full list."""
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_tools=frozenset({"read"}),
        )
    )
    tools = ["read", "write", "anything"]
    assert scope.filter_tools("a", tools) == tools


def test_filter_tools_no_policy_raises(scope: ScopeModule) -> None:
    """Unknown agent fails closed with PolicyNotRegistered (v0.5.1 fix).

    Prior to v0.5.1 this method returned the full tool list unchanged
    when no policy was registered — an unintentional bypass that let
    ``hidden_tools`` leak to unregistered agents.  The fix mirrors
    ``check()``'s default-deny contract.
    """
    with pytest.raises(PolicyNotRegistered):
        scope.filter_tools("unknown", ["a", "b"])


@pytest.mark.asyncio
async def test_hidden_tools_also_blocked_by_check(scope: ScopeModule) -> None:
    """Hidden tools not in allowed_tools are blocked by scope.check()."""
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_tools=frozenset({"read"}),
            hidden_tools=frozenset({"secret_tool"}),
        )
    )
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="a", tool="secret_tool")


def test_policy_with_both_allowed_and_hidden(scope: ScopeModule) -> None:
    """Allowed and hidden tools should work together correctly."""
    scope.register(
        ScopePolicy(
            agent_id="a",
            allowed_tools=frozenset({"read", "write", "list"}),
            hidden_tools=frozenset({"write"}),
        )
    )
    tools = ["read", "write", "list", "delete"]
    result = scope.filter_tools("a", tools)
    assert "write" not in result
    assert "read" in result
    assert "list" in result


# ---------------------------------------------------------------------------
# Exploit: case-sensitivity bypass
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_case_sensitive_tool_check(scope: ScopeModule) -> None:
    """Register policy with 'read_file', check 'READ_FILE' — must be denied.

    This proves an attacker cannot bypass the whitelist by changing case.
    Tool matching is case-sensitive by design (frozenset membership test).
    """
    scope.register(
        ScopePolicy(agent_id="case-test", allowed_tools=frozenset({"read_file"}))
    )
    # Exact match works
    await scope.check(agent_id="case-test", tool="read_file")
    # Case variations must all be denied
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="case-test", tool="READ_FILE")
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="case-test", tool="Read_File")
    with pytest.raises(ScopeViolation):
        await scope.check(agent_id="case-test", tool="READ_file")


# ---------------------------------------------------------------------------
# Exploit: SQL injection in tool name
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sql_injection_in_tool_name(scope: ScopeModule) -> None:
    """SQL injection payload as tool name must raise ScopeViolation, not
    execute the SQL."""
    scope.register(
        ScopePolicy(agent_id="sqli", allowed_tools=frozenset({"safe_tool"}))
    )
    with pytest.raises(ScopeViolation):
        await scope.check(
            agent_id="sqli",
            tool="'; DROP TABLE governance_audit_events; --",
        )
