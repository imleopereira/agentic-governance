"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { LiveBadge } from "@/components/LiveBadge";
import { CardSkeleton, TableSkeleton } from "@/components/Skeleton";
import { formatUtcTimestamp } from "@/lib/formatDate";

/**
 * Built-in model pricing reference (mirrors the SDK's pricing.py).
 * Prices in USD per 1M tokens.
 */
const MODEL_PRICING: Record<string, { input: number; output: number }> = {
  "gpt-4o": { input: 2.5, output: 10.0 },
  "gpt-4o-mini": { input: 0.15, output: 0.6 },
  "gpt-4-turbo": { input: 10.0, output: 30.0 },
  "o1": { input: 15.0, output: 60.0 },
  "o3-mini": { input: 1.1, output: 4.4 },
  "claude-opus-4-6": { input: 15.0, output: 75.0 },
  "claude-sonnet-4-6": { input: 3.0, output: 15.0 },
  "claude-haiku-4-5": { input: 0.8, output: 4.0 },
  "claude-3.5-sonnet": { input: 3.0, output: 15.0 },
  "gemini-2.0-flash": { input: 0.1, output: 0.4 },
  "gemini-1.5-pro": { input: 3.5, output: 10.5 },
  "mistral-large": { input: 2.0, output: 6.0 },
};

function PricingReference() {
  const [open, setOpen] = useState(false);

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="text-xs transition-colors flex items-center gap-1.5"
        style={{ color: "var(--accent)" }}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          className="transition-transform"
          style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)" }}
        >
          <path d="M4 2l4 4-4 4" />
        </svg>
        {open ? "Hide pricing reference" : "View model pricing reference"}
        {!open && (
          <span style={{ color: "var(--text-tertiary)" }}>
            - {Object.keys(MODEL_PRICING).length} models
          </span>
        )}
      </button>
      {open && (
        <div className="mt-3 overflow-x-auto animate-fade-in-up">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left border-b" style={{ borderColor: "var(--border)", color: "var(--text-tertiary)" }}>
                <th className="py-1.5 pr-3 font-medium uppercase tracking-wider">Model</th>
                <th className="py-1.5 pr-3 font-medium uppercase tracking-wider text-right">Input ($/1M)</th>
                <th className="py-1.5 pr-3 font-medium uppercase tracking-wider text-right">Output ($/1M)</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(MODEL_PRICING).map(([model, prices], idx) => (
                <tr
                  key={model}
                  className="border-b"
                  style={{
                    borderColor: "var(--border)",
                    background: idx % 2 === 1 ? "rgba(255, 255, 255, 0.02)" : "transparent",
                  }}
                >
                  <td className="py-1.5 pr-3 font-mono">{model}</td>
                  <td className="py-1.5 pr-3 text-right font-mono">${prices.input.toFixed(2)}</td>
                  <td className="py-1.5 pr-3 text-right font-mono">${prices.output.toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs mt-2" style={{ color: "var(--text-tertiary)" }}>
            Prices are used for automatic cost estimation. Unknown models are zero-costed.
            Prefix matching is used for versioned model names.
          </p>
        </div>
      )}
    </div>
  );
}

export default function CostPage() {
  const { data: agents, isLoading: agentsLoading } = useQuery({
    queryKey: ["cost-agents"],
    queryFn: api.costAgents,
    refetchInterval: 10_000,
  });
  const { data: sessions, isLoading: sessionsLoading } = useQuery({
    queryKey: ["cost-sessions"],
    queryFn: () => api.costSessions(),
    refetchInterval: 10_000,
  });
  const { data: models, isLoading: modelsLoading } = useQuery({
    queryKey: ["cost-models"],
    queryFn: api.costModels,
    refetchInterval: 10_000,
  });

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight mb-1">Cost Dashboard</h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            Per-agent and per-session spend tracking for today (UTC).
          </p>
        </div>
        <LiveBadge />
      </div>

      <section>
        <h2 className="text-lg font-semibold mb-3">Agent Daily Spend</h2>
        {agentsLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <CardSkeleton key={i} />
            ))}
          </div>
        ) : agents?.length === 0 ? (
          <div
            className="text-center py-8 border border-dashed"
            style={{ borderColor: "var(--border)", borderRadius: "var(--radius-md)" }}
          >
            <p style={{ color: "var(--text-tertiary)" }}>No agent spend tracked today.</p>
            <p className="text-xs mt-1" style={{ color: "var(--text-tertiary)" }}>
              Cost tracking starts automatically when agents make LLM calls.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {agents?.map((a) => {
              const DAILY_BUDGET = 10; // $10 soft daily budget threshold
              const pct = Math.min((a.usd_used_today / DAILY_BUDGET) * 100, 100);
              const overBudget = a.usd_used_today > DAILY_BUDGET;
              const barColor = overBudget
                ? "var(--danger)"
                : pct > 75
                  ? "var(--warn)"
                  : "var(--accent)";
              return (
                <div
                  key={a.agent_id}
                  className="border p-4"
                  style={{
                    borderColor: overBudget ? "rgba(239, 68, 68, 0.3)" : "var(--border)",
                    background: "var(--card)",
                    borderRadius: "var(--radius-md)",
                  }}
                >
                  <p className="font-semibold text-sm truncate">{a.agent_id}</p>
                  <div className="mt-2 flex justify-between text-sm">
                    <span style={{ color: "var(--text-tertiary)" }}>USD today</span>
                    <span className="font-mono">${a.usd_used_today.toFixed(4)}</span>
                  </div>
                  {/* Spend progress bar */}
                  <div className="mt-1.5 mb-2">
                    <div
                      className="h-1.5 w-full overflow-hidden"
                      style={{
                        background: "rgba(255, 255, 255, 0.06)",
                        borderRadius: "9999px",
                      }}
                    >
                      <div
                        className="h-full transition-all duration-500"
                        style={{
                          width: `${pct}%`,
                          background: barColor,
                          borderRadius: "9999px",
                        }}
                      />
                    </div>
                    <div className="flex justify-between mt-0.5">
                      <span className="text-[10px]" style={{ color: "var(--text-tertiary)" }}>
                        {pct.toFixed(0)}% of ${DAILY_BUDGET} daily budget
                      </span>
                      {overBudget && (
                        <span className="text-[10px] font-medium" style={{ color: "var(--danger)" }}>
                          Over budget
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span style={{ color: "var(--text-tertiary)" }}>Tokens today</span>
                    <span className="font-mono">{a.tokens_used_today.toLocaleString()}</span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">Session Spend (top 50)</h2>
        {sessionsLoading ? (
          <TableSkeleton rows={8} />
        ) : sessions?.length === 0 ? (
          <div
            className="text-center py-8 border border-dashed"
            style={{ borderColor: "var(--border)", borderRadius: "var(--radius-md)" }}
          >
            <p style={{ color: "var(--text-tertiary)" }}>No session spend recorded yet.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left border-b" style={{ borderColor: "var(--border)", color: "var(--text-tertiary)" }}>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Agent</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Session</th>
                  <th className="py-2 pr-3 text-right font-medium text-xs uppercase tracking-wider">USD</th>
                  <th className="py-2 pr-3 text-right font-medium text-xs uppercase tracking-wider">Tokens</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Last updated</th>
                </tr>
              </thead>
              <tbody>
                {sessions?.map((s, idx) => (
                  <tr
                    key={`${s.agent_id}-${s.session_id}`}
                    className="border-b hover:bg-white/[0.06] transition-colors"
                    style={{
                      borderColor: "var(--border)",
                      background: idx % 2 === 1 ? "rgba(255, 255, 255, 0.02)" : "transparent",
                    }}
                  >
                    <td className="py-2 pr-3">{s.agent_id}</td>
                    <td className="py-2 pr-3">
                      <span
                        className="font-mono text-xs truncate inline-block max-w-[8rem] align-bottom"
                        title={s.session_id}
                        style={{ color: "var(--accent)" }}
                      >
                        {s.session_id.slice(0, 12)}...
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">
                      ${s.usd_used.toFixed(6)}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">
                      {s.tokens_used.toLocaleString()}
                    </td>
                    <td className="py-2 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
                      {s.last_updated ? formatUtcTimestamp(s.last_updated) : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">Model Breakdown (today)</h2>
        {modelsLoading ? (
          <TableSkeleton rows={5} />
        ) : !models || models.length === 0 ? (
          <div
            className="text-center py-8 border border-dashed"
            style={{ borderColor: "var(--border)", borderRadius: "var(--radius-md)" }}
          >
            <p style={{ color: "var(--text-tertiary)" }}>No per-model cost data yet.</p>
            <p className="text-xs mt-1" style={{ color: "var(--text-tertiary)" }}>
              Model-level tracking requires the model field on audit events.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            {(() => {
              const maxSpend = Math.max(...models.map((m) => m.usd_used_today), 0.000001);
              return (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left border-b" style={{ borderColor: "var(--border)", color: "var(--text-tertiary)" }}>
                      <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Model</th>
                      <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Agent</th>
                      <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider min-w-[140px]">Spend</th>
                      <th className="py-2 pr-3 text-right font-medium text-xs uppercase tracking-wider">USD</th>
                      <th className="py-2 pr-3 text-right font-medium text-xs uppercase tracking-wider">Tokens</th>
                      <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Last updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.map((m, i) => {
                      const barPct = (m.usd_used_today / maxSpend) * 100;
                      return (
                        <tr
                          key={`${m.agent_id}-${m.model}-${i}`}
                          className="border-b hover:bg-white/[0.06] transition-colors"
                          style={{
                            borderColor: "var(--border)",
                            background: i % 2 === 1 ? "rgba(255, 255, 255, 0.02)" : "transparent",
                          }}
                        >
                          <td className="py-2 pr-3 font-mono text-xs">{m.model}</td>
                          <td className="py-2 pr-3">{m.agent_id}</td>
                          <td className="py-2 pr-3">
                            <div
                              className="h-2 w-full overflow-hidden"
                              style={{
                                background: "rgba(255, 255, 255, 0.06)",
                                borderRadius: "9999px",
                              }}
                            >
                              <div
                                className="h-full transition-all duration-500"
                                style={{
                                  width: `${barPct}%`,
                                  background: "var(--accent)",
                                  borderRadius: "9999px",
                                }}
                              />
                            </div>
                          </td>
                          <td className="py-2 pr-3 text-right font-mono">
                            ${m.usd_used_today.toFixed(6)}
                          </td>
                          <td className="py-2 pr-3 text-right font-mono">
                            {m.tokens_used_today.toLocaleString()}
                          </td>
                          <td className="py-2 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
                            {m.last_updated ? formatUtcTimestamp(m.last_updated) : "-"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              );
            })()}
          </div>
        )}
      </section>

      <section
        className="border p-4"
        style={{
          borderColor: "var(--border)",
          background: "var(--card)",
          borderRadius: "var(--radius-md)",
        }}
      >
        <h2 className="text-sm font-semibold mb-2">Model Pricing Reference</h2>
        <p className="text-xs mb-2" style={{ color: "var(--text-tertiary)" }}>
          The SDK automatically estimates costs using built-in pricing for major providers.
        </p>
        <PricingReference />
      </section>
    </div>
  );
}
