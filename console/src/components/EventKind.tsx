"use client";

/**
 * Color-coded event kind labels using the Code Atelier palette.
 * Uses the violet accent for primary actions, semantic colors for outcomes.
 */
const KIND_STYLES: Record<string, { color: string; bg: string; bold?: boolean }> = {
  "llm.call": { color: "var(--accent-light)", bg: "rgba(155, 79, 255, 0.12)" },
  "llm.result": { color: "var(--accent)", bg: "rgba(130, 40, 245, 0.12)" },
  "llm.error": { color: "var(--danger)", bg: "rgba(239, 68, 68, 0.12)" },
  "tool.call": { color: "#60A5FA", bg: "rgba(96, 165, 250, 0.12)" },
  "tool.result": { color: "#93C5FD", bg: "rgba(147, 197, 253, 0.10)" },
  "tool.error": { color: "var(--danger)", bg: "rgba(239, 68, 68, 0.12)" },
  "scope.violation": { color: "#FCA5A5", bg: "rgba(239, 68, 68, 0.18)", bold: true },
  "budget.exceeded": { color: "#FCA5A5", bg: "rgba(239, 68, 68, 0.18)", bold: true },
  "budget.check_failed": { color: "var(--danger)", bg: "rgba(239, 68, 68, 0.12)" },
  "approval.requested": { color: "#FDE68A", bg: "rgba(234, 179, 8, 0.14)" },
  "approval.granted": { color: "#86EFAC", bg: "rgba(34, 197, 94, 0.14)" },
  "approval.denied": { color: "#FCA5A5", bg: "rgba(239, 68, 68, 0.14)" },
  "chain.degraded_start": { color: "#FDBA74", bg: "rgba(249, 115, 22, 0.14)" },
  "agent.start": { color: "var(--accent-light)", bg: "rgba(155, 79, 255, 0.12)" },
  "agent.end": { color: "var(--accent)", bg: "rgba(130, 40, 245, 0.12)" },
};

/** Infer a fallback style from the kind prefix for unknown kinds. */
function inferStyle(kind: string): { color: string; bg: string } {
  if (kind.startsWith("scope.")) return { color: "#FCA5A5", bg: "rgba(239, 68, 68, 0.14)" };
  if (kind.startsWith("approval.")) return { color: "#FDE68A", bg: "rgba(234, 179, 8, 0.14)" };
  if (kind.startsWith("tool.")) return { color: "#60A5FA", bg: "rgba(96, 165, 250, 0.12)" };
  if (kind.startsWith("budget.")) return { color: "#FDBA74", bg: "rgba(249, 115, 22, 0.14)" };
  if (kind.startsWith("llm.")) return { color: "var(--accent-light)", bg: "rgba(155, 79, 255, 0.12)" };
  return { color: "var(--fg)", bg: "rgba(255, 255, 255, 0.06)" };
}

export function EventKind({ kind }: { kind: string }) {
  const style = KIND_STYLES[kind] ?? inferStyle(kind);
  return (
    <span
      className={`inline-block text-xs font-mono px-2 py-0.5 ${style.bold ? "font-semibold" : ""}`}
      style={{
        color: style.color,
        background: style.bg,
        borderRadius: "9999px",
      }}
    >
      {kind}
    </span>
  );
}
