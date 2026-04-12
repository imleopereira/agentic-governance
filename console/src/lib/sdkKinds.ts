/**
 * SDK_KINDS — canonical audit event kinds re-derived from the Python source.
 *
 * DO NOT invent kinds here. Every entry below is cited to a Python file:line
 * where the SDK actually emits it. Re-verify in 30 seconds by grepping:
 *
 *   rg 'kind\s*=\s*"[a-z][a-z_]+\.' src/codeatelier_governance
 *
 * Sources grepped (2026-04-11):
 *   src/codeatelier_governance/audit/models.py           L36-L47  (EventKind enum)
 *   src/codeatelier_governance/audit/module.py           L308,L320
 *   src/codeatelier_governance/scope/module.py           L328
 *   src/codeatelier_governance/cost/module.py            L373,L432,L479
 *   src/codeatelier_governance/gates/module.py           L144,L184
 *   src/codeatelier_governance/contracts/module.py       L116,L153
 *   src/codeatelier_governance/loop/module.py            L314
 *   src/codeatelier_governance/routing/module.py         L143,L237,L348,L625,L644
 *   src/codeatelier_governance/integrations/openai_wrap.py    L149,L157,L179,L192 (llm.call, llm.error, llm.result)
 *   src/codeatelier_governance/integrations/anthropic_wrap.py L154,L162,L182,L196
 *   src/codeatelier_governance/integrations/langchain_handler.py L220,L252,L273,L296,L316,L334,L355,L372,L390 (llm.*, tool.*, chain.start/end/error)
 *
 * CTO "phantom kinds" rejection list: cost.tracked, agent.halted, agent.resumed
 * — NOT FOUND in grep, so NOT included. NOTE: `llm.error` IS emitted (see
 * openai_wrap:157, anthropic_wrap:162, langchain_handler:273) even though the
 * CTO's memo listed it as phantom; grep wins, so it stays with a citation.
 *
 * Missing from CTO's "real" list:
 *   - none; all twelve CTO-listed kinds were found and are included below.
 */

export const SDK_KINDS = {
  // --- agent lifecycle (audit/models.py L36-L37) ---
  AGENT_START: "agent.start",
  AGENT_END: "agent.end",

  // --- tool events (audit/models.py L38-L39, langchain_handler.py L296,L316,L334) ---
  TOOL_CALL: "tool.call",
  TOOL_RESULT: "tool.result",
  TOOL_ERROR: "tool.error",

  // --- llm events (audit/models.py L40-L41, openai_wrap.py L149,L157,L179) ---
  LLM_CALL: "llm.call",
  LLM_RESULT: "llm.result",
  LLM_ERROR: "llm.error",

  // --- scope (scope/module.py L328) ---
  SCOPE_VIOLATION: "scope.violation",

  // --- budget / cost (cost/module.py L373,L432,L479) ---
  BUDGET_EXCEEDED: "budget.exceeded",
  BUDGET_CHECK_FAILED: "budget.check_failed",

  // --- HITL gates (gates/module.py L144,L184) ---
  APPROVAL_REQUESTED: "approval.requested",
  APPROVAL_GRANTED: "approval.granted",
  APPROVAL_DENIED: "approval.denied",

  // --- contracts (contracts/module.py L116,L153) ---
  CONTRACT_PRE_VIOLATION: "contract.pre_violation",
  CONTRACT_POST_VIOLATION: "contract.post_violation",

  // --- loop detection (loop/module.py L314) ---
  LOOP_DETECTED: "loop.detected",

  // --- chain integrity (audit/module.py L308,L320) ---
  CHAIN_DEGRADED_START: "chain.degraded_start",
  // langchain_handler emits chain.start/end/error but these are LangChain
  // orchestration events, not HMAC chain events; kept for completeness:
  CHAIN_START: "chain.start",
  CHAIN_END: "chain.end",
  CHAIN_ERROR: "chain.error",

  // --- routing (routing/module.py L143,L237,L348,L625,L644) ---
  ROUTING_POLICY_CHANGED: "routing.policy_changed",
  ROUTING_SUGGESTION: "routing.suggestion",
  ROUTING_SUGGEST_FAILED: "routing.suggest_failed",

  // --- custom (audit/models.py L47) ---
  CUSTOM: "custom",
} as const;

export type SdkKind = (typeof SDK_KINDS)[keyof typeof SDK_KINDS];

/** Set of kinds that represent an action being BLOCKED by a gate. */
export const BLOCKING_KINDS: ReadonlySet<SdkKind> = new Set<SdkKind>([
  SDK_KINDS.SCOPE_VIOLATION,
  SDK_KINDS.BUDGET_EXCEEDED,
  SDK_KINDS.CONTRACT_PRE_VIOLATION,
  SDK_KINDS.CONTRACT_POST_VIOLATION,
  SDK_KINDS.APPROVAL_DENIED,
]);

/** Prefix helpers for gate inference. */
export function isScopeKind(kind: string): boolean {
  return kind.startsWith("scope.");
}
export function isBudgetKind(kind: string): boolean {
  return kind.startsWith("budget.");
}
export function isApprovalKind(kind: string): boolean {
  return kind.startsWith("approval.");
}
export function isContractKind(kind: string): boolean {
  return kind.startsWith("contract.");
}
export function isRoutingKind(kind: string): boolean {
  return kind.startsWith("routing.");
}
export function isLoopKind(kind: string): boolean {
  return kind.startsWith("loop.");
}
