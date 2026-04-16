/**
 * 100 % branch coverage on `getInferredGate` — the pure gate-inference
 * helper extracted into `EnforcementTraceImpl.tsx` for F8.
 *
 * Each test constructs a minimal `AuditEvent` that hits exactly one of
 * the 8 conditional branches in the function and asserts the inferred
 * 5-gate vector. The audit-gate side-effect (`hmac_value` truthy →
 * "pass", falsy → "warn") is also covered to pin the auditPass fork.
 */
import { describe, it, expect } from "vitest";
import {
  getInferredGate,
  getInferredIntegration,
} from "./EnforcementTraceImpl";
import type { AuditEvent } from "../../lib/api";
import { SDK_KINDS } from "../../lib/sdkKinds";

function makeEvent(overrides: Partial<AuditEvent> = {}): AuditEvent {
  return {
    event_id: "evt-1",
    session_id: "sess-1",
    agent_id: "agent-a",
    parent_event_id: null,
    kind: SDK_KINDS.TOOL_CALL,
    model: null,
    input_hash: null,
    output_hash: null,
    metadata: {},
    prev_hash: null,
    hmac_value: "deadbeef",
    chain_seq: 1,
    created_at: "2026-04-15T12:00:00.000Z",
    ...overrides,
  };
}

describe("getInferredGate — 8 conditional branches", () => {
  it("branch 1: scope.violation → scope FAIL, downstream not_reached", () => {
    const g = getInferredGate(makeEvent({ kind: SDK_KINDS.SCOPE_VIOLATION }));
    expect(g).toEqual({
      scope: "fail",
      budget: "not_reached",
      hitl: "not_reached",
      contract: "not_reached",
      audit: "pass",
    });
  });

  it("branch 2a: budget.exceeded → budget FAIL", () => {
    const g = getInferredGate(makeEvent({ kind: SDK_KINDS.BUDGET_EXCEEDED }));
    expect(g.budget).toBe("fail");
    expect(g.scope).toBe("pass");
    expect(g.hitl).toBe("not_reached");
  });

  it("branch 2b: budget.check_failed → budget FAIL", () => {
    const g = getInferredGate(
      makeEvent({ kind: SDK_KINDS.BUDGET_CHECK_FAILED }),
    );
    expect(g.budget).toBe("fail");
  });

  it("branch 3a: contract.pre_violation → contract FAIL, HITL skip", () => {
    const g = getInferredGate(
      makeEvent({ kind: SDK_KINDS.CONTRACT_PRE_VIOLATION }),
    );
    expect(g).toEqual({
      scope: "pass",
      budget: "pass",
      hitl: "skip",
      contract: "fail",
      audit: "pass",
    });
  });

  it("branch 3b: contract.post_violation → contract FAIL", () => {
    const g = getInferredGate(
      makeEvent({ kind: SDK_KINDS.CONTRACT_POST_VIOLATION }),
    );
    expect(g.contract).toBe("fail");
  });

  it("branch 4: approval.requested → HITL pending, contract not_reached", () => {
    const g = getInferredGate(
      makeEvent({ kind: SDK_KINDS.APPROVAL_REQUESTED }),
    );
    expect(g.hitl).toBe("pending");
    expect(g.contract).toBe("not_reached");
  });

  it("branch 5: approval.denied → HITL fail", () => {
    const g = getInferredGate(makeEvent({ kind: SDK_KINDS.APPROVAL_DENIED }));
    expect(g.hitl).toBe("fail");
    expect(g.contract).toBe("not_reached");
  });

  it("branch 6: approval.granted → HITL pass", () => {
    const g = getInferredGate(makeEvent({ kind: SDK_KINDS.APPROVAL_GRANTED }));
    expect(g.hitl).toBe("pass");
    expect(g.contract).toBe("skip");
  });

  it("branch 7: chain.degraded_start → all skip, audit warn", () => {
    const g = getInferredGate(
      makeEvent({ kind: SDK_KINDS.CHAIN_DEGRADED_START, hmac_value: null }),
    );
    expect(g).toEqual({
      scope: "skip",
      budget: "skip",
      hitl: "skip",
      contract: "skip",
      audit: "warn",
    });
  });

  it("branch 8a: routing.* → all pass", () => {
    const g = getInferredGate(
      makeEvent({ kind: SDK_KINDS.ROUTING_POLICY_CHANGED }),
    );
    expect(g.scope).toBe("pass");
    expect(g.budget).toBe("pass");
    expect(g.audit).toBe("pass");
  });

  it("branch 8b: default tool.call → all pass", () => {
    const g = getInferredGate(makeEvent({ kind: SDK_KINDS.TOOL_CALL }));
    expect(g.scope).toBe("pass");
    expect(g.budget).toBe("pass");
  });
});

describe("getInferredGate — audit-gate fork (hmac_value)", () => {
  it("audit = pass when hmac_value is truthy", () => {
    const g = getInferredGate(makeEvent({ hmac_value: "abc123" }));
    expect(g.audit).toBe("pass");
  });

  it("audit = warn when hmac_value is null", () => {
    const g = getInferredGate(makeEvent({ hmac_value: null }));
    expect(g.audit).toBe("warn");
  });

  it("audit = warn when hmac_value is empty string", () => {
    const g = getInferredGate(makeEvent({ hmac_value: "" }));
    expect(g.audit).toBe("warn");
  });
});

describe("getInferredIntegration — coverage mode", () => {
  it("llm.call with model → wrap_openai/wrap_anthropic", () => {
    const r = getInferredIntegration(
      makeEvent({ kind: SDK_KINDS.LLM_CALL, model: "gpt-4" }),
    );
    expect(r.fullCoverage).toBe(true);
  });

  it("chain.start (not degraded) → LangChain handler", () => {
    const r = getInferredIntegration(
      makeEvent({ kind: SDK_KINDS.CHAIN_START }),
    );
    expect(r.mode).toContain("LangChain");
  });

  it("routing.* → routing policy (advisory)", () => {
    const r = getInferredIntegration(
      makeEvent({ kind: SDK_KINDS.ROUTING_SUGGESTION }),
    );
    expect(r.fullCoverage).toBe(true);
  });

  it("bare tool.call → manual (limited)", () => {
    const r = getInferredIntegration(makeEvent({ kind: SDK_KINDS.TOOL_CALL }));
    expect(r.fullCoverage).toBe(false);
  });
});
