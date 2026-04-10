"use client";

const KIND_COLORS: Record<string, string> = {
  "llm.call": "text-blue-400",
  "llm.result": "text-blue-300",
  "llm.error": "text-red-400",
  "tool.call": "text-purple-400",
  "tool.result": "text-purple-300",
  "tool.error": "text-red-400",
  "scope.violation": "text-red-500 font-medium",
  "budget.exceeded": "text-red-500 font-medium",
  "budget.check_failed": "text-red-400",
  "approval.requested": "text-yellow-400",
  "approval.granted": "text-green-400",
  "approval.denied": "text-red-400",
  "chain.degraded_start": "text-orange-400",
  "agent.start": "text-cyan-400",
  "agent.end": "text-cyan-300",
};

export function EventKind({ kind }: { kind: string }) {
  const color = KIND_COLORS[kind] ?? "text-[var(--fg)]";
  return <span className={color}>{kind}</span>;
}
