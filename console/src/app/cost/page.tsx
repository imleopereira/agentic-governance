"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export default function CostPage() {
  const { data: agents, isLoading: agentsLoading } = useQuery({
    queryKey: ["cost-agents"],
    queryFn: api.costAgents,
  });
  const { data: sessions, isLoading: sessionsLoading } = useQuery({
    queryKey: ["cost-sessions"],
    queryFn: () => api.costSessions(),
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold mb-1">Cost Dashboard</h1>
        <p className="text-sm text-[var(--muted)]">
          Per-agent and per-session spend tracking for today (UTC).
        </p>
      </div>

      <section>
        <h2 className="text-lg font-semibold mb-3">Agent Daily Spend</h2>
        {agentsLoading ? (
          <p className="text-[var(--muted)]">Loading...</p>
        ) : agents?.length === 0 ? (
          <p className="text-[var(--muted)]">No agent spend tracked today.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {agents?.map((a) => (
              <div
                key={a.agent_id}
                className="rounded-lg border border-[var(--border)] bg-[var(--card)] p-4"
              >
                <p className="font-semibold text-sm truncate">{a.agent_id}</p>
                <div className="mt-2 flex justify-between text-sm">
                  <span className="text-[var(--muted)]">USD today</span>
                  <span className="font-mono">
                    ${a.usd_used_today.toFixed(4)}
                  </span>
                </div>
                <div className="flex justify-between text-sm">
                  <span className="text-[var(--muted)]">Tokens today</span>
                  <span className="font-mono">
                    {a.tokens_used_today.toLocaleString()}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">Session Spend (top 50)</h2>
        {sessionsLoading ? (
          <p className="text-[var(--muted)]">Loading...</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[var(--muted)] border-b border-[var(--border)]">
                  <th className="py-2 pr-3">Agent</th>
                  <th className="py-2 pr-3">Session</th>
                  <th className="py-2 pr-3 text-right">USD</th>
                  <th className="py-2 pr-3 text-right">Tokens</th>
                  <th className="py-2 pr-3">Last updated</th>
                </tr>
              </thead>
              <tbody>
                {sessions?.map((s) => (
                  <tr
                    key={`${s.agent_id}-${s.session_id}`}
                    className="border-b border-[var(--border)] hover:bg-white/5"
                  >
                    <td className="py-2 pr-3">{s.agent_id}</td>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {s.session_id.slice(0, 8)}...
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">
                      ${s.usd_used.toFixed(6)}
                    </td>
                    <td className="py-2 pr-3 text-right font-mono">
                      {s.tokens_used.toLocaleString()}
                    </td>
                    <td className="py-2 pr-3 text-xs text-[var(--muted)]">
                      {s.last_updated
                        ? new Date(s.last_updated).toLocaleString()
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
