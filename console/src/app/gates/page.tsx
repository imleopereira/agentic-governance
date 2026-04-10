"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { TimeAgo } from "@/components/TimeAgo";
import { LiveBadge } from "@/components/LiveBadge";
import { TableSkeleton, CardSkeleton } from "@/components/Skeleton";

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <button
      onClick={copy}
      className="text-xs px-2 py-1 rounded border border-[var(--border)] text-[var(--muted)] hover:text-[var(--accent)] hover:border-[var(--accent)] transition"
    >
      {copied ? "Copied!" : label}
    </button>
  );
}

export default function GatesPage() {
  const { data: pending, isLoading: pendingLoading } = useQuery({
    queryKey: ["gates-pending"],
    queryFn: api.gatesPending,
  });
  const { data: recent, isLoading: recentLoading } = useQuery({
    queryKey: ["gates-recent"],
    queryFn: () => api.gatesRecent(50),
  });

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold mb-1">Approval Gates</h1>
          <p className="text-sm text-[var(--muted)]">
            Pending and recently resolved human-in-the-loop approvals.
          </p>
        </div>
        <LiveBadge />
      </div>

      <section>
        <div className="flex items-center gap-2 mb-3">
          <h2 className="text-lg font-semibold">Pending Approvals</h2>
          {pending && pending.length > 0 && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-yellow-900/50 text-yellow-400 border border-yellow-800">
              {pending.length}
            </span>
          )}
        </div>
        {pendingLoading ? (
          <div className="space-y-2">
            <CardSkeleton />
            <CardSkeleton />
          </div>
        ) : pending?.length === 0 ? (
          <div className="text-center py-8 rounded-lg border border-dashed border-[var(--border)]">
            <p className="text-[var(--muted)]">No pending approvals</p>
            <p className="text-xs text-[var(--muted)] mt-1">
              Agents requesting HITL approval will appear here.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {pending?.map((p) => (
              <div
                key={p.request_id}
                className="rounded-lg border border-yellow-800/50 bg-[var(--card)] p-4"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <p className="font-semibold text-sm">{p.kind}</p>
                      <StatusBadge status="WARN" />
                    </div>
                    <p className="text-xs text-[var(--muted)]">
                      Agent: <span className="text-[var(--fg)]">{p.agent_id}</span>
                      {" | "}
                      Request: <span className="font-mono">{p.request_id.slice(0, 12)}...</span>
                    </p>
                    <p className="text-xs text-[var(--muted)] mt-1">
                      Created: <TimeAgo iso={p.created_at} />
                      {" | "}
                      Expires: <TimeAgo iso={p.expires_at} />
                    </p>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <CopyButton
                      text={`await sdk.gates.grant("${p.request_id}")`}
                      label="Copy grant cmd"
                    />
                    <CopyButton
                      text={p.request_id}
                      label="Copy request ID"
                    />
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">Recent Resolutions</h2>
        {recentLoading ? (
          <TableSkeleton rows={5} />
        ) : recent?.length === 0 ? (
          <p className="text-[var(--muted)] text-center py-8">
            No approvals resolved yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[var(--muted)] border-b border-[var(--border)]">
                  <th className="py-2 pr-3">Kind</th>
                  <th className="py-2 pr-3">Agent</th>
                  <th className="py-2 pr-3">Resolution</th>
                  <th className="py-2 pr-3">Request ID</th>
                  <th className="py-2 pr-3">Resolved</th>
                </tr>
              </thead>
              <tbody>
                {recent?.map((g) => (
                  <tr
                    key={g.request_id}
                    className="border-b border-[var(--border)] hover:bg-white/5 transition-colors"
                  >
                    <td className="py-2 pr-3">{g.kind}</td>
                    <td className="py-2 pr-3">{g.agent_id}</td>
                    <td className="py-2 pr-3">
                      <StatusBadge
                        status={g.resolution === "granted" ? "PASS" : "FAIL"}
                      />
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">
                      {g.request_id.slice(0, 12)}...
                    </td>
                    <td className="py-2 pr-3 text-xs text-[var(--muted)]">
                      <TimeAgo iso={g.resolved_at} />
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
