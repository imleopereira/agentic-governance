"use client";

import { useState, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
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
      className="text-xs px-2 py-1 border transition-colors"
      style={{
        borderColor: copied ? "var(--accent)" : "var(--border)",
        color: copied ? "var(--accent)" : "var(--text-tertiary)",
        borderRadius: "var(--radius-sm)",
      }}
    >
      {copied ? "Copied!" : label}
    </button>
  );
}

function GateActionButtons({ requestId }: { requestId: string }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<{
    loading: "grant" | "deny" | null;
    error: string | null;
    success: string | null;
  }>({ loading: null, error: null, success: null });

  const handleAction = useCallback(
    async (action: "grant" | "deny") => {
      setStatus({ loading: action, error: null, success: null });
      try {
        const result =
          action === "grant"
            ? await api.grantGate(requestId)
            : await api.denyGate(requestId);
        setStatus({
          loading: null,
          error: null,
          success: `${result.resolution.charAt(0).toUpperCase() + result.resolution.slice(1)} successfully`,
        });
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ["gates-pending"] }),
          queryClient.invalidateQueries({ queryKey: ["gates-recent"] }),
        ]);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Unknown error occurred";
        setStatus({ loading: null, error: message, success: null });
      }
    },
    [requestId, queryClient]
  );

  if (status.success) {
    return (
      <span className="text-xs font-medium" style={{ color: "var(--success)" }}>
        {status.success}
      </span>
    );
  }

  return (
    <div className="flex flex-col gap-1.5 items-end">
      <div className="flex gap-1.5">
        <button
          onClick={() => handleAction("grant")}
          disabled={status.loading !== null}
          className="text-xs px-3 py-1 border font-medium disabled:opacity-50 transition-colors"
          style={{
            borderColor: "rgba(34, 197, 94, 0.4)",
            background: "rgba(34, 197, 94, 0.08)",
            color: "var(--success)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {status.loading === "grant" ? "Granting..." : "Grant"}
        </button>
        <button
          onClick={() => handleAction("deny")}
          disabled={status.loading !== null}
          className="text-xs px-3 py-1 border font-medium disabled:opacity-50 transition-colors"
          style={{
            borderColor: "rgba(239, 68, 68, 0.4)",
            background: "rgba(239, 68, 68, 0.08)",
            color: "var(--danger)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {status.loading === "deny" ? "Denying..." : "Deny"}
        </button>
      </div>
      {status.error && (
        <span className="text-xs max-w-[200px] truncate" style={{ color: "var(--danger)" }} title={status.error}>
          {status.error}
        </span>
      )}
    </div>
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
          <h1 className="text-2xl font-bold tracking-tight mb-1">Approval Gates</h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            Pending and recently resolved human-in-the-loop approvals.
          </p>
        </div>
        <LiveBadge />
      </div>

      <section>
        <div className="flex items-center gap-2 mb-3">
          <h2 className="text-lg font-semibold">Pending Approvals</h2>
          {pending && pending.length > 0 && (
            <span
              className="text-xs px-2 py-0.5 border font-medium"
              style={{
                background: "rgba(234, 179, 8, 0.1)",
                borderColor: "rgba(234, 179, 8, 0.3)",
                color: "var(--warn)",
                borderRadius: "var(--radius-sm)",
              }}
            >
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
          <div
            className="text-center py-8 border border-dashed"
            style={{ borderColor: "var(--border)", borderRadius: "var(--radius-md)" }}
          >
            <p style={{ color: "var(--text-tertiary)" }}>No pending approvals</p>
            <p className="text-xs mt-1" style={{ color: "var(--text-tertiary)" }}>
              Agents requesting HITL approval will appear here.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {pending?.map((p) => (
              <div
                key={p.request_id}
                className="border p-4"
                style={{
                  borderColor: "rgba(234, 179, 8, 0.2)",
                  background: "var(--card)",
                  borderRadius: "var(--radius-md)",
                }}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1">
                    <div className="flex items-center gap-2 mb-1">
                      <p className="font-semibold text-sm">{p.kind}</p>
                      <StatusBadge status="WARN" />
                    </div>
                    <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
                      Agent: <span style={{ color: "var(--fg)" }}>{p.agent_id}</span>
                      {" | "}
                      Request: <span className="font-mono">{p.request_id.slice(0, 12)}...</span>
                    </p>
                    <p className="text-xs mt-1" style={{ color: "var(--text-tertiary)" }}>
                      Created: <TimeAgo iso={p.created_at} />
                      {" | "}
                      Expires: <TimeAgo iso={p.expires_at} />
                    </p>
                  </div>
                  <div className="flex flex-col gap-1.5 items-end">
                    <GateActionButtons requestId={p.request_id} />
                    <CopyButton text={p.request_id} label="Copy request ID" />
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
          <div
            className="text-center py-8 border border-dashed"
            style={{ borderColor: "var(--border)", borderRadius: "var(--radius-md)" }}
          >
            <p style={{ color: "var(--text-tertiary)" }}>No approvals resolved yet.</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left border-b" style={{ borderColor: "var(--border)", color: "var(--text-tertiary)" }}>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Kind</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Agent</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Resolution</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Request ID</th>
                  <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Resolved</th>
                </tr>
              </thead>
              <tbody>
                {recent?.map((g) => (
                  <tr
                    key={g.request_id}
                    className="border-b hover:bg-white/[0.03] transition-colors"
                    style={{ borderColor: "var(--border)" }}
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
                    <td className="py-2 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
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
