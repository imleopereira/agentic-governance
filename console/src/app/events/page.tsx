"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type VerifyResult } from "@/lib/api";
import { EventKind } from "@/components/EventKind";
import { MetadataViewer } from "@/components/MetadataViewer";
import { TimeAgo } from "@/components/TimeAgo";
import { LiveBadge } from "@/components/LiveBadge";
import { TableSkeleton } from "@/components/Skeleton";

function VerifyButton({ sessionId }: { sessionId: string }) {
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [loading, setLoading] = useState(false);

  async function verify() {
    setLoading(true);
    try {
      const r = await api.verifySession(sessionId);
      setResult(r);
    } catch {
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  if (result) {
    return (
      <span
        className={`text-xs font-medium ${result.verified ? "text-green-400" : "text-red-400"}`}
      >
        {result.verified
          ? `Chain verified (${result.event_count} events)`
          : `TAMPERED at ${result.first_failure?.slice(0, 8)}...`}
      </span>
    );
  }

  return (
    <button
      onClick={verify}
      disabled={loading}
      data-tour="verify"
      className="text-xs px-3 py-1.5 rounded-lg border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-50 transition"
    >
      {loading ? "Verifying..." : "Verify chain integrity"}
    </button>
  );
}

export default function EventsPage() {
  const [agentFilter, setAgentFilter] = useState(
    typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("agent_id") ?? ""
      : ""
  );
  const [kindFilter, setKindFilter] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");

  const { data: events, isLoading } = useQuery({
    queryKey: ["events", agentFilter, kindFilter, sessionFilter],
    queryFn: () =>
      api.events({
        agent_id: agentFilter || undefined,
        kind: kindFilter || undefined,
        session_id: sessionFilter || undefined,
        limit: 200,
      }),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Audit Log Explorer</h1>
        <LiveBadge />
      </div>

      <div className="flex flex-wrap gap-3 text-sm">
        <input
          placeholder="Filter by agent_id"
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="bg-[var(--card)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm w-48 focus:border-[var(--accent)] outline-none transition"
        />
        <input
          placeholder="Filter by kind"
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
          className="bg-[var(--card)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm w-48 focus:border-[var(--accent)] outline-none transition"
        />
        <input
          placeholder="Filter by session_id"
          value={sessionFilter}
          onChange={(e) => setSessionFilter(e.target.value)}
          className="bg-[var(--card)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm w-64 focus:border-[var(--accent)] outline-none transition"
        />
        {(agentFilter || kindFilter || sessionFilter) && (
          <button
            onClick={() => {
              setAgentFilter("");
              setKindFilter("");
              setSessionFilter("");
            }}
            className="text-xs text-[var(--muted)] hover:text-white transition"
          >
            Clear filters
          </button>
        )}
      </div>

      {sessionFilter && (
        <div className="flex items-center gap-3 p-3 rounded-lg bg-[var(--card)] border border-[var(--border)]">
          <span className="text-sm text-[var(--muted)]">Session {sessionFilter.slice(0, 8)}...:</span>
          <VerifyButton sessionId={sessionFilter} />
        </div>
      )}

      {isLoading ? (
        <TableSkeleton rows={10} />
      ) : !events || events.length === 0 ? (
        <div className="text-center py-12">
          <p className="text-[var(--muted)]">
            {agentFilter || kindFilter || sessionFilter
              ? "No events match your filters."
              : "No audit events yet. Start logging from the SDK."}
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[var(--muted)] border-b border-[var(--border)]">
                <th className="py-2 pr-3 w-12">#</th>
                <th className="py-2 pr-3">Kind</th>
                <th className="py-2 pr-3">Agent</th>
                <th className="py-2 pr-3">Session</th>
                <th className="py-2 pr-3">Metadata</th>
                <th className="py-2 pr-3 w-24">Time</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr
                  key={e.event_id}
                  className="border-b border-[var(--border)] hover:bg-white/5 transition-colors"
                >
                  <td className="py-2 pr-3 text-[var(--muted)] text-xs">
                    {e.chain_seq}
                  </td>
                  <td className="py-2 pr-3">
                    <EventKind kind={e.kind} />
                  </td>
                  <td className="py-2 pr-3 text-sm">{e.agent_id}</td>
                  <td className="py-2 pr-3">
                    <button
                      onClick={() => setSessionFilter(e.session_id)}
                      className="text-[var(--accent)] hover:underline font-mono text-xs"
                    >
                      {e.session_id.slice(0, 8)}...
                    </button>
                  </td>
                  <td className="py-2 pr-3 max-w-xs">
                    <MetadataViewer data={e.metadata as Record<string, unknown>} />
                  </td>
                  <td className="py-2 pr-3 text-xs text-[var(--muted)]">
                    <TimeAgo iso={e.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs text-[var(--muted)] mt-2">
            Showing {events.length} events (most recent first)
          </p>
        </div>
      )}
    </div>
  );
}
