"""Tests for the behavioral contracts module."""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from codeatelier_governance.audit import InMemoryAuditStore
from codeatelier_governance.audit.models import AuditEvent
from codeatelier_governance.audit.module import AuditModule
from codeatelier_governance.contracts import (
    Contract,
    ContractsModule,
    ContractViolation,
    PostCondition,
    PreCondition,
)
from codeatelier_governance.cost import BudgetPolicy, CostModule
from codeatelier_governance.gates.module import GatesModule
from codeatelier_governance.scope import ScopeModule, ScopePolicy


# ---------------------------------------------------------------------------
# 1. Register a contract and verify it's stored
# ---------------------------------------------------------------------------
def test_register_and_get_contract(contracts: ContractsModule) -> None:
    contract = Contract(
        agent_id="a",
        tool="read_file",
        pre=[PreCondition(check="scope_allowed", message="Must be in scope")],
    )
    contracts.register(contract)
    assert contracts.get_contract("a", "read_file") is contract


# ---------------------------------------------------------------------------
# 2. Pre-condition scope_allowed passes when tool is in scope
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pre_scope_allowed_passes(
    contracts: ContractsModule,
    scope: ScopeModule,
) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"send_email"})))
    contract = Contract(
        agent_id="a",
        tool="send_email",
        pre=[PreCondition(check="scope_allowed", message="Tool must be in scope")],
    )
    contracts.register(contract)
    sid = uuid4()
    await contracts.check_pre("a", sid, "send_email")  # should not raise


# ---------------------------------------------------------------------------
# 3. Pre-condition scope_allowed fails when tool is NOT in scope
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pre_scope_allowed_fails(
    contracts: ContractsModule,
    scope: ScopeModule,
) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"read_file"})))
    contract = Contract(
        agent_id="a",
        tool="delete_db",
        pre=[PreCondition(check="scope_allowed", message="Tool must be in scope")],
    )
    contracts.register(contract)
    sid = uuid4()
    with pytest.raises(ContractViolation, match="delete_db"):
        await contracts.check_pre("a", sid, "delete_db")


# ---------------------------------------------------------------------------
# 4. Pre-condition budget_available passes when budget is OK
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pre_budget_available_passes(
    contracts: ContractsModule,
    cost: CostModule,
) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=100.0))
    contract = Contract(
        agent_id="a",
        tool="charge",
        pre=[PreCondition(check="budget_available", message="Budget check")],
    )
    contracts.register(contract)
    sid = uuid4()
    await contracts.check_pre("a", sid, "charge")  # should not raise


# ---------------------------------------------------------------------------
# 5. Pre-condition budget_available fails when budget exceeded
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pre_budget_available_fails(
    contracts: ContractsModule,
    cost: CostModule,
) -> None:
    cost.register(BudgetPolicy(agent_id="a", per_session_usd=1.0))
    sid = uuid4()
    # Blow the budget
    await cost.track("a", sid, usd=2.0)
    contract = Contract(
        agent_id="a",
        tool="charge",
        pre=[PreCondition(check="budget_available", message="Budget must not be exceeded")],
    )
    contracts.register(contract)
    with pytest.raises(ContractViolation, match="budget_available"):
        await contracts.check_pre("a", sid, "charge")


# ---------------------------------------------------------------------------
# 6. Post-condition audit_logged passes when event exists
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_audit_logged_passes(
    contracts: ContractsModule,
    audit: AuditModule,
) -> None:
    sid = uuid4()
    # Log an audit event with the tool in metadata
    await audit.log(
        AuditEvent(
            agent_id="a",
            session_id=sid,
            kind="tool.call",
            metadata={"tool": "charge_customer"},
        )
    )
    # Give the batching writer time to flush
    await asyncio.sleep(0.1)
    contract = Contract(
        agent_id="a",
        tool="charge_customer",
        post=[PostCondition(check="audit_logged", message="Must be logged")],
    )
    contracts.register(contract)
    await contracts.check_post("a", sid, "charge_customer")  # should not raise


# ---------------------------------------------------------------------------
# 7. Contract with no pre-conditions passes check_pre
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_pre_conditions_passes(contracts: ContractsModule) -> None:
    contract = Contract(agent_id="a", tool="noop")
    contracts.register(contract)
    sid = uuid4()
    await contracts.check_pre("a", sid, "noop")  # should not raise


# ---------------------------------------------------------------------------
# 8. Contract with no post-conditions passes check_post
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_post_conditions_passes(contracts: ContractsModule) -> None:
    contract = Contract(agent_id="a", tool="noop")
    contracts.register(contract)
    sid = uuid4()
    await contracts.check_post("a", sid, "noop")  # should not raise


# ---------------------------------------------------------------------------
# 9. ContractViolation includes tool name and condition message
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_violation_includes_tool_and_message(
    contracts: ContractsModule,
    scope: ScopeModule,
) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"safe"})))
    contract = Contract(
        agent_id="a",
        tool="dangerous",
        pre=[PreCondition(check="scope_allowed", message="Nope, not allowed")],
    )
    contracts.register(contract)
    sid = uuid4()
    with pytest.raises(ContractViolation) as exc_info:
        await contracts.check_pre("a", sid, "dangerous")
    msg = str(exc_info.value)
    assert "dangerous" in msg
    assert "Nope, not allowed" in msg


# ---------------------------------------------------------------------------
# 10. Audit event emitted on pre-condition violation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_event_on_pre_violation(
    contracts: ContractsModule,
    scope: ScopeModule,
    audit_store: InMemoryAuditStore,
) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"safe"})))
    contract = Contract(
        agent_id="a",
        tool="bad_tool",
        pre=[PreCondition(check="scope_allowed", message="Not in scope")],
    )
    contracts.register(contract)
    sid = uuid4()
    with pytest.raises(ContractViolation):
        await contracts.check_pre("a", sid, "bad_tool")
    await asyncio.sleep(0.1)
    events = list(audit_store._events.values())
    violations = [e for e in events if e.kind == "contract.pre_violation"]
    assert len(violations) >= 1
    assert violations[0].metadata["tool"] == "bad_tool"
    assert violations[0].metadata["check"] == "scope_allowed"


# ---------------------------------------------------------------------------
# 11. Audit event emitted on post-condition violation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_event_on_post_violation(
    contracts: ContractsModule,
    audit_store: InMemoryAuditStore,
) -> None:
    sid = uuid4()
    contract = Contract(
        agent_id="a",
        tool="missing_tool",
        post=[PostCondition(check="audit_logged", message="Must be logged")],
    )
    contracts.register(contract)
    with pytest.raises(ContractViolation):
        await contracts.check_post("a", sid, "missing_tool")
    await asyncio.sleep(0.1)
    events = list(audit_store._events.values())
    violations = [e for e in events if e.kind == "contract.post_violation"]
    assert len(violations) >= 1
    assert violations[0].metadata["tool"] == "missing_tool"
    assert violations[0].metadata["check"] == "audit_logged"


# ---------------------------------------------------------------------------
# 12. Custom check registration and execution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_custom_check_registration_and_execution(
    contracts: ContractsModule,
) -> None:
    async def always_pass(agent_id: str, session_id: object, tool: str) -> bool:
        return True

    contracts.register_check("my_check", always_pass)
    contract = Contract(
        agent_id="a",
        tool="custom_tool",
        pre=[PreCondition(check="custom", message="Custom gate", params={"callable": "my_check"})],
    )
    contracts.register(contract)
    sid = uuid4()
    await contracts.check_pre("a", sid, "custom_tool")  # should not raise


# ---------------------------------------------------------------------------
# 13. Unknown agent/tool returns None (no enforcement)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_agent_tool_no_enforcement(
    contracts: ContractsModule,
) -> None:
    assert contracts.get_contract("ghost", "unknown") is None
    sid = uuid4()
    # check_pre and check_post should be no-ops
    await contracts.check_pre("ghost", sid, "unknown")
    await contracts.check_post("ghost", sid, "unknown")


# ---------------------------------------------------------------------------
# 14. list_contracts returns all contracts for an agent
# ---------------------------------------------------------------------------
def test_list_contracts(contracts: ContractsModule) -> None:
    c1 = Contract(agent_id="a", tool="t1")
    c2 = Contract(agent_id="a", tool="t2")
    c3 = Contract(agent_id="b", tool="t3")
    contracts.register(c1)
    contracts.register(c2)
    contracts.register(c3)
    result = contracts.list_contracts("a")
    assert len(result) == 2
    tools = {c.tool for c in result}
    assert tools == {"t1", "t2"}


# ---------------------------------------------------------------------------
# 15. enforce context manager runs pre and post
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enforce_context_manager(
    contracts: ContractsModule,
    scope: ScopeModule,
    audit: AuditModule,
) -> None:
    scope.register(ScopePolicy(agent_id="a", allowed_tools=frozenset({"do_thing"})))
    sid = uuid4()
    # Log an event to satisfy the audit_logged post-condition
    await audit.log(
        AuditEvent(
            agent_id="a",
            session_id=sid,
            kind="tool.call",
            metadata={"tool": "do_thing"},
        )
    )
    await asyncio.sleep(0.1)
    contract = Contract(
        agent_id="a",
        tool="do_thing",
        pre=[PreCondition(check="scope_allowed", message="In scope")],
        post=[PostCondition(check="audit_logged", message="Must be logged")],
    )
    contracts.register(contract)
    async with contracts.enforce("a", sid, "do_thing"):
        pass  # Tool call happens here


# ---------------------------------------------------------------------------
# 16. Custom check that fails raises ContractViolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_custom_check_failure(
    contracts: ContractsModule,
) -> None:
    async def always_fail(agent_id: str, session_id: object, tool: str) -> bool:
        return False

    contracts.register_check("fail_check", always_fail)
    contract = Contract(
        agent_id="a",
        tool="guarded",
        pre=[PreCondition(check="custom", message="Must pass custom", params={"callable": "fail_check"})],
    )
    contracts.register(contract)
    sid = uuid4()
    with pytest.raises(ContractViolation, match="custom"):
        await contracts.check_pre("a", sid, "guarded")


# ---------------------------------------------------------------------------
# 17. hitl_approved pre-condition with granted gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hitl_approved_with_granted_gate(
    contracts: ContractsModule,
    gates: GatesModule,
) -> None:
    # Create and grant an approval
    req = await gates.request("charge", "a", payload={"amount": 100})
    await gates.grant(req.token)

    contract = Contract(
        agent_id="a",
        tool="charge",
        pre=[PreCondition(check="hitl_approved", message="Needs human approval")],
    )
    contracts.register(contract)
    sid = uuid4()
    await contracts.check_pre("a", sid, "charge")  # should not raise


# ---------------------------------------------------------------------------
# 18. hitl_approved fails when no gate is granted
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hitl_approved_fails_without_grant(
    contracts: ContractsModule,
) -> None:
    contract = Contract(
        agent_id="a",
        tool="charge",
        pre=[PreCondition(check="hitl_approved", message="Needs human approval")],
    )
    contracts.register(contract)
    sid = uuid4()
    with pytest.raises(ContractViolation, match="hitl_approved"):
        await contracts.check_pre("a", sid, "charge")
