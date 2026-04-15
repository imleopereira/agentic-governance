/**
 * EnforcementTraceImpl — pure gate-inference helpers for the 5-gate chain
 * rendered by `EnforcementTrace.tsx`.
 *
 * File ownership note (v0.6):
 *   `EnforcementTrace.tsx` is the presentational component (owned by F1).
 *   This file (`EnforcementTraceImpl.tsx`) is the testable core — pure
 *   functions with zero React surface area — owned by F8. Extracting the
 *   inference path into a separate module lets the F8 gate-inference test
 *   achieve 100 % branch coverage on each of the conditional branches in
 *   `getInferredGate` without mounting a React tree (the console test
 *   environment is Node-only; no jsdom, no @testing-library/react).
 *
 * The functions below are a re-derivation of the same rules implemented
 * inline in `EnforcementTrace.tsx :: inferGates`, keyed on `event.kind`
 * per the .agent-outputs/ui-ux/enforcement-trace-spec.md section 2
 * "Implementation note (Security)". The wiring of the component to use
 * these helpers (instead of its inline copy) is deferred to a follow-up
 * patch — extracting the helpers is enough to unlock the test, and
 * swapping the component wiring in the same F8 change would collide with
 * F1's concurrent edits to `EnforcementTrace.tsx`.
 */

import type { AuditEvent } from "../../lib/api";
import { SDK_KINDS, isRoutingKind } from "../../lib/sdkKinds";

export type GateId = "scope" | "budget" | "hitl" | "contract" | "audit";
export type GateStatus =
  | "pass"
  | "fail"
  | "skip"
  | "pending"
  | "not_reached"
  | "warn";

export interface InferredGate {
  scope: GateStatus;
  budget: GateStatus;
  hitl: GateStatus;
  contract: GateStatus;
  audit: GateStatus;
}

/**
 * Derive the 5-gate status vector from a single audit event.
 *
 * Branch enumeration (must match `EnforcementTrace.tsx :: inferGates`):
 *
 *   1. `scope.violation`              → scope FAIL, downstream not_reached
 *   2. `budget.exceeded` / `.check_failed` → budget FAIL, downstream not_reached
 *   3. `contract.{pre,post}_violation`     → contract FAIL, HITL skip
 *   4. `approval.requested`                → HITL pending, contract not_reached
 *   5. `approval.denied`                   → HITL fail, contract not_reached
 *   6. `approval.granted`                  → all pass, HITL=pass
 *   7. `chain.degraded_start`              → all skip, audit warn
 *   8. default (tool.*, llm.*, routing.*)  → all pass
 *
 * The audit gate is derived separately: it is `pass` when `hmac_value` is
 * truthy, `warn` otherwise (chain-degraded observation event). The
 * `chain.degraded_start` branch overrides this to `warn` unconditionally.
 */
export function getInferredGate(event: AuditEvent): InferredGate {
  const kind = event.kind;
  const auditPass: GateStatus = event.hmac_value ? "pass" : "warn";

  // 1. scope.violation
  if (kind === SDK_KINDS.SCOPE_VIOLATION) {
    return {
      scope: "fail",
      budget: "not_reached",
      hitl: "not_reached",
      contract: "not_reached",
      audit: auditPass,
    };
  }

  // 2. budget.exceeded / budget.check_failed
  if (
    kind === SDK_KINDS.BUDGET_EXCEEDED ||
    kind === SDK_KINDS.BUDGET_CHECK_FAILED
  ) {
    return {
      scope: "pass",
      budget: "fail",
      hitl: "not_reached",
      contract: "not_reached",
      audit: auditPass,
    };
  }

  // 3. contract.{pre,post}_violation
  if (
    kind === SDK_KINDS.CONTRACT_PRE_VIOLATION ||
    kind === SDK_KINDS.CONTRACT_POST_VIOLATION
  ) {
    return {
      scope: "pass",
      budget: "pass",
      hitl: "skip",
      contract: "fail",
      audit: auditPass,
    };
  }

  // 4. approval.requested
  if (kind === SDK_KINDS.APPROVAL_REQUESTED) {
    return {
      scope: "pass",
      budget: "pass",
      hitl: "pending",
      contract: "not_reached",
      audit: auditPass,
    };
  }

  // 5. approval.denied
  if (kind === SDK_KINDS.APPROVAL_DENIED) {
    return {
      scope: "pass",
      budget: "pass",
      hitl: "fail",
      contract: "not_reached",
      audit: auditPass,
    };
  }

  // 6. approval.granted
  if (kind === SDK_KINDS.APPROVAL_GRANTED) {
    return {
      scope: "pass",
      budget: "pass",
      hitl: "pass",
      contract: "skip",
      audit: auditPass,
    };
  }

  // 7. chain.degraded_start
  if (kind === SDK_KINDS.CHAIN_DEGRADED_START) {
    return {
      scope: "skip",
      budget: "skip",
      hitl: "skip",
      contract: "skip",
      audit: "warn",
    };
  }

  // 8. routing.* and everything else: all pass
  if (isRoutingKind(kind)) {
    return {
      scope: "pass",
      budget: "pass",
      hitl: "skip",
      contract: "skip",
      audit: auditPass,
    };
  }

  return {
    scope: "pass",
    budget: "pass",
    hitl: "skip",
    contract: "skip",
    audit: auditPass,
  };
}

/**
 * Infer integration-mode metadata for the UI badge.
 * Pure; safe to unit test. See `EnforcementTrace.tsx :: inferIntegration`.
 */
export function getInferredIntegration(event: AuditEvent): {
  mode: string;
  fullCoverage: boolean;
} {
  const kind = event.kind;
  if (kind.startsWith("llm.") && event.model) {
    return {
      mode: "wrap_openai / wrap_anthropic (full coverage)",
      fullCoverage: true,
    };
  }
  if (kind.startsWith("chain.") && kind !== SDK_KINDS.CHAIN_DEGRADED_START) {
    return { mode: "LangChain handler (callback coverage)", fullCoverage: true };
  }
  if (isRoutingKind(kind)) {
    return { mode: "routing policy (advisory)", fullCoverage: true };
  }
  return { mode: "manual (limited coverage)", fullCoverage: false };
}
