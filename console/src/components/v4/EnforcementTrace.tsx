"use client";

/**
 * EnforcementTrace — 5-gate inline card (Scope -> Budget -> HITL -> Contract -> Audit).
 *
 * Gate status is INFERRED from the event's `kind` field (SDK-controlled, unforgeable).
 * Rules per .agent-outputs/ui-ux/enforcement-trace-spec.md section 2.
 *
 * This component is presentational — it does NOT fetch session events itself.
 * Inference is keyed strictly on the single expanded event's `kind`, which is
 * sufficient for all violation variants and the common PASS-all case. The
 * previous `sessionEvents` prop was declared but never destructured (dead
 * API surface) and was removed in F1.
 */

import { memo, useMemo } from "react";
import type { AuditEvent } from "@/lib/api";
import {
  SDK_KINDS,
  isRoutingKind,
  type SdkKind,
} from "@/lib/sdkKinds";
import { Pill, type PillTone } from "./Pill";
import { SectionLabel } from "./SectionLabel";

type GateId = "scope" | "budget" | "hitl" | "contract" | "audit";
type GateStatus = "pass" | "fail" | "skip" | "pending" | "not_reached" | "warn";

interface Gate {
  id: GateId;
  label: string;
  status: GateStatus;
  detail: string;
}

export interface EnforcementTraceProps {
  event: AuditEvent;
}

const GATE_ORDER: readonly GateId[] = [
  "scope",
  "budget",
  "hitl",
  "contract",
  "audit",
] as const;

const GATE_LABELS: Record<GateId, string> = {
  scope: "Scope",
  budget: "Budget",
  hitl: "HITL",
  contract: "Contract",
  audit: "Audit",
};

const STATUS_TONE: Record<GateStatus, PillTone> = {
  pass: "success",
  fail: "danger",
  skip: "skip",
  pending: "warn",
  not_reached: "skip",
  warn: "warn",
};

const STATUS_LABEL: Record<GateStatus, string> = {
  pass: "PASS",
  fail: "FAIL",
  skip: "SKIP",
  pending: "PEND",
  not_reached: "--",
  warn: "WARN",
};

/**
 * Primary inference. Keyed strictly on `event.kind` (SDK-internal, unforgeable)
 * per spec section 2 "Implementation note (Security)".
 */
function inferGates(event: AuditEvent): Gate[] {
  const kind = event.kind as SdkKind | string;
  const auditPass: Gate = {
    id: "audit",
    label: GATE_LABELS.audit,
    status: event.hmac_value ? "pass" : "warn",
    detail: event.hmac_value
      ? `HMAC chain_seq=${event.chain_seq}, hash verified`
      : "chain degraded - HMAC not computed",
  };

  const defaults: Record<GateId, Gate> = {
    scope: {
      id: "scope",
      label: GATE_LABELS.scope,
      status: "pass",
      detail: "no scope violation in session",
    },
    budget: {
      id: "budget",
      label: GATE_LABELS.budget,
      status: "pass",
      detail: "within session and daily caps",
    },
    hitl: {
      id: "hitl",
      label: GATE_LABELS.hitl,
      status: "skip",
      detail: "no approval policy registered",
    },
    contract: {
      id: "contract",
      label: GATE_LABELS.contract,
      status: "skip",
      detail: "no contract registered for this action",
    },
    audit: auditPass,
  };

  // scope.violation -> Scope FAIL, downstream not_reached
  if (kind === SDK_KINDS.SCOPE_VIOLATION) {
    const tool = (event.metadata?.tool as string | undefined) ?? "(unknown)";
    return [
      {
        id: "scope",
        label: GATE_LABELS.scope,
        status: "fail",
        detail: `tool "${tool}" NOT in allowlist; ScopeViolation raised`,
      },
      {
        id: "budget",
        label: GATE_LABELS.budget,
        status: "not_reached",
        detail: "blocked at Scope",
      },
      {
        id: "hitl",
        label: GATE_LABELS.hitl,
        status: "not_reached",
        detail: "not reached",
      },
      {
        id: "contract",
        label: GATE_LABELS.contract,
        status: "not_reached",
        detail: "not reached",
      },
      auditPass,
    ];
  }

  // budget.exceeded / budget.check_failed -> Budget FAIL
  if (
    kind === SDK_KINDS.BUDGET_EXCEEDED ||
    kind === SDK_KINDS.BUDGET_CHECK_FAILED
  ) {
    const cap = event.metadata?.cap ?? event.metadata?.limit;
    const used = event.metadata?.used;
    const detail =
      cap !== undefined && used !== undefined
        ? `$${String(used)} / $${String(cap)} cap exceeded`
        : "budget cap exceeded; BudgetExceeded raised";
    return [
      { ...defaults.scope },
      {
        id: "budget",
        label: GATE_LABELS.budget,
        status: "fail",
        detail,
      },
      {
        id: "hitl",
        label: GATE_LABELS.hitl,
        status: "not_reached",
        detail: "not reached",
      },
      {
        id: "contract",
        label: GATE_LABELS.contract,
        status: "not_reached",
        detail: "not reached",
      },
      auditPass,
    ];
  }

  // contract.{pre,post}_violation -> Contract FAIL, HITL SKIP
  if (
    kind === SDK_KINDS.CONTRACT_PRE_VIOLATION ||
    kind === SDK_KINDS.CONTRACT_POST_VIOLATION
  ) {
    const phase =
      kind === SDK_KINDS.CONTRACT_PRE_VIOLATION ? "precondition" : "postcondition";
    return [
      { ...defaults.scope },
      { ...defaults.budget },
      {
        id: "hitl",
        label: GATE_LABELS.hitl,
        status: "skip",
        detail: "not required before contract check",
      },
      {
        id: "contract",
        label: GATE_LABELS.contract,
        status: "fail",
        detail: `${phase} failed; ContractViolation raised`,
      },
      auditPass,
    ];
  }

  // approval.requested -> HITL PEND, Contract not_reached
  if (kind === SDK_KINDS.APPROVAL_REQUESTED) {
    const requestId = (event.metadata?.request_id as string | undefined) ?? "";
    return [
      { ...defaults.scope },
      { ...defaults.budget },
      {
        id: "hitl",
        label: GATE_LABELS.hitl,
        status: "pending",
        detail: requestId
          ? `approval requested, waiting... (${requestId})`
          : "approval requested, waiting...",
      },
      {
        id: "contract",
        label: GATE_LABELS.contract,
        status: "not_reached",
        detail: "pending HITL resolution",
      },
      auditPass,
    ];
  }

  // approval.denied -> HITL FAIL
  if (kind === SDK_KINDS.APPROVAL_DENIED) {
    return [
      { ...defaults.scope },
      { ...defaults.budget },
      {
        id: "hitl",
        label: GATE_LABELS.hitl,
        status: "fail",
        detail: "approval denied by operator",
      },
      {
        id: "contract",
        label: GATE_LABELS.contract,
        status: "not_reached",
        detail: "blocked at HITL",
      },
      auditPass,
    ];
  }

  // approval.granted -> all pass, HITL=pass
  if (kind === SDK_KINDS.APPROVAL_GRANTED) {
    return [
      { ...defaults.scope },
      { ...defaults.budget },
      {
        id: "hitl",
        label: GATE_LABELS.hitl,
        status: "pass",
        detail: "approval granted by operator",
      },
      { ...defaults.contract },
      auditPass,
    ];
  }

  // loop.detected -> observational; all pass
  if (kind === SDK_KINDS.LOOP_DETECTED) {
    return [
      { ...defaults.scope },
      { ...defaults.budget },
      { ...defaults.hitl },
      { ...defaults.contract },
      {
        ...auditPass,
        detail: auditPass.detail + " (loop observation)",
      },
    ];
  }

  // chain.degraded_start -> Audit WARN, others skip
  if (kind === SDK_KINDS.CHAIN_DEGRADED_START) {
    return [
      { ...defaults.scope, status: "skip", detail: "chain degraded" },
      { ...defaults.budget, status: "skip", detail: "chain degraded" },
      { ...defaults.hitl, status: "skip", detail: "chain degraded" },
      { ...defaults.contract, status: "skip", detail: "chain degraded" },
      {
        id: "audit",
        label: GATE_LABELS.audit,
        status: "warn",
        detail: "audit chain entered degraded mode (DB unreachable)",
      },
    ];
  }

  // routing.* -> all pass, routing info shown in integration badge
  if (isRoutingKind(kind)) {
    return [
      { ...defaults.scope },
      { ...defaults.budget },
      { ...defaults.hitl },
      { ...defaults.contract },
      auditPass,
    ];
  }

  // Default: normal tool.call / llm.call / llm.result -> all pass
  return GATE_ORDER.map((id) => defaults[id]);
}

/** Infer integration mode from the event kind. */
function inferIntegration(event: AuditEvent): {
  mode: string;
  fullCoverage: boolean;
} {
  const kind = event.kind;
  if (kind.startsWith("llm.") && event.model) {
    return { mode: "wrap_openai / wrap_anthropic (full coverage)", fullCoverage: true };
  }
  if (kind.startsWith("chain.") && kind !== SDK_KINDS.CHAIN_DEGRADED_START) {
    return { mode: "LangChain handler (callback coverage)", fullCoverage: true };
  }
  if (isRoutingKind(kind)) {
    return { mode: "routing policy (advisory)", fullCoverage: true };
  }
  return { mode: "manual (limited coverage)", fullCoverage: false };
}

function EnforcementTraceImpl({ event }: EnforcementTraceProps) {
  const gates = useMemo(() => inferGates(event), [event]);
  const integration = useMemo(() => inferIntegration(event), [event]);
  const isBlocked = gates.some((g) => g.status === "fail");

  return (
    <section
      role="region"
      aria-label={`Enforcement trace for event ${event.event_id}`}
      className="rounded-lg border p-4 text-sm"
      style={{
        borderColor: "var(--border)",
        background: "rgba(0,0,0,0.15)",
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <SectionLabel>Enforcement Trace</SectionLabel>
        {isBlocked && (
          <Pill tone="danger" title="This action was blocked by enforcement">
            BLOCKED
          </Pill>
        )}
      </div>

      {/* Action summary */}
      <div className="mb-4 text-xs" style={{ color: "var(--text-secondary)" }}>
        <div className="font-mono">
          <span style={{ color: "var(--fg)" }}>{event.kind}</span>
          <span className="mx-2">|</span>
          <span>agent: {event.agent_id}</span>
          {event.metadata?.tool !== undefined && (
            <>
              <span className="mx-2">|</span>
              <span>tool: {String(event.metadata.tool)}</span>
            </>
          )}
          {event.model && (
            <>
              <span className="mx-2">|</span>
              <span>{event.model}</span>
            </>
          )}
        </div>
        <div className="font-mono mt-1">
          <span>session: {event.session_id.slice(0, 8)}...</span>
          <span className="mx-2">|</span>
          <span>chain_seq: {event.chain_seq}</span>
        </div>
      </div>

      {/* Gate chain */}
      <SectionLabel>Gate Chain</SectionLabel>
      <ol
        role="list"
        aria-label="Enforcement gate chain"
        className="space-y-2 mb-4"
      >
        {gates.map((gate, idx) => (
          <li
            key={gate.id}
            role="listitem"
            className="flex items-start gap-3"
          >
            <span
              aria-hidden="true"
              className="font-mono text-xs w-6 text-right"
              style={{ color: "var(--text-tertiary)" }}
            >
              [{idx + 1}]
            </span>
            <span
              className="w-20 text-xs font-medium"
              style={{ color: "var(--fg)" }}
            >
              {gate.label}
            </span>
            <Pill tone={STATUS_TONE[gate.status]}>
              <span
                aria-label={`${gate.label} gate: ${gate.status}`}
              >
                {STATUS_LABEL[gate.status]}
              </span>
            </Pill>
            <span
              className="flex-1 text-xs"
              style={{ color: "var(--text-secondary)" }}
            >
              {gate.detail}
            </span>
          </li>
        ))}
      </ol>

      {/* Integration badge */}
      <SectionLabel>Integration</SectionLabel>
      <div
        className="text-xs mb-1"
        style={{ color: "var(--fg)" }}
      >
        {integration.mode}
      </div>
      {!integration.fullCoverage && (
        <div className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          Only gates explicitly called in application code are enforced.
        </div>
      )}
    </section>
  );
}

export const EnforcementTrace = memo(EnforcementTraceImpl);
EnforcementTrace.displayName = "EnforcementTrace";
