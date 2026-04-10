"use client";

/**
 * Color-coded event kind labels using the Code Atelier palette.
 * Uses the violet accent for primary actions, semantic colors for outcomes.
 */
const KIND_STYLES: Record<string, { color: string; bold?: boolean }> = {
  "llm.call": { color: "var(--accent-light)" },
  "llm.result": { color: "var(--accent)" },
  "llm.error": { color: "var(--danger)" },
  "tool.call": { color: "#93C5D7" },  // brand cyan
  "tool.result": { color: "#7BB3C7" },
  "tool.error": { color: "var(--danger)" },
  "scope.violation": { color: "var(--danger)", bold: true },
  "budget.exceeded": { color: "var(--danger)", bold: true },
  "budget.check_failed": { color: "var(--danger)" },
  "approval.requested": { color: "var(--warn)" },
  "approval.granted": { color: "var(--success)" },
  "approval.denied": { color: "var(--danger)" },
  "chain.degraded_start": { color: "#F97316" },
  "agent.start": { color: "var(--accent-light)" },
  "agent.end": { color: "var(--accent)" },
};

export function EventKind({ kind }: { kind: string }) {
  const style = KIND_STYLES[kind];
  return (
    <span
      className={`text-xs font-mono ${style?.bold ? "font-semibold" : ""}`}
      style={{ color: style?.color ?? "var(--fg)" }}
    >
      {kind}
    </span>
  );
}
