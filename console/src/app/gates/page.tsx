"use client";

import { useState, useCallback, useEffect, useRef } from "react";
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

function ExpiryCountdown({ expiresAt }: { expiresAt: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const expiryMs = new Date(expiresAt).getTime();
  const remainingMs = Math.max(0, expiryMs - now);
  const totalMs = expiryMs - now;

  const minutes = Math.floor(remainingMs / 60_000);
  const seconds = Math.floor((remainingMs % 60_000) / 1000);

  // Calculate urgency: < 2 min = critical, < 5 min = warn, else = normal
  const isExpired = remainingMs <= 0;
  const isCritical = !isExpired && minutes < 2;
  const isWarning = !isExpired && !isCritical && minutes < 5;

  const barColor = isExpired
    ? "var(--danger)"
    : isCritical
      ? "var(--danger)"
      : isWarning
        ? "var(--warn)"
        : "var(--success)";

  // Progress as fraction of a 10-min window (reasonable default)
  const progressFraction = isExpired ? 0 : Math.min(1, remainingMs / 600_000);

  const timeLabel = isExpired
    ? "Expired"
    : `${minutes}m ${seconds.toString().padStart(2, "0")}s remaining`;

  return (
    <div className="flex items-center gap-2 mt-1.5">
      <div
        className="flex-1 h-1 overflow-hidden"
        style={{
          background: "rgba(255, 255, 255, 0.06)",
          borderRadius: "2px",
          maxWidth: "120px",
        }}
      >
        <div
          className="h-full transition-all"
          style={{
            width: `${progressFraction * 100}%`,
            background: barColor,
            borderRadius: "2px",
          }}
        />
      </div>
      <span
        className="text-xs font-medium tabular-nums"
        style={{
          color: barColor,
          ...(isCritical ? { animation: "pulse-ring 2s ease-in-out infinite" } : {}),
        }}
      >
        {timeLabel}
      </span>
    </div>
  );
}

function GateActionButtons({ requestId }: { requestId: string }) {
  const queryClient = useQueryClient();
  const [confirmDeny, setConfirmDeny] = useState(false);
  const confirmTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [status, setStatus] = useState<{
    loading: "grant" | "deny" | null;
    error: string | null;
    success: string | null;
  }>({ loading: null, error: null, success: null });

  useEffect(() => {
    return () => {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
    };
  }, []);

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

  function handleDenyClick() {
    if (!confirmDeny) {
      setConfirmDeny(true);
      confirmTimeoutRef.current = setTimeout(() => setConfirmDeny(false), 3000);
    } else {
      setConfirmDeny(false);
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      handleAction("deny");
    }
  }

  if (status.success) {
    return (
      <span className="text-xs font-medium" style={{ color: "var(--success)" }}>
        {status.success}
      </span>
    );
  }

  return (
    <div className="flex flex-col gap-1.5 items-end">
      <div className="flex gap-2">
        <button
          onClick={() => handleAction("grant")}
          disabled={status.loading !== null}
          className="text-sm px-4 py-1.5 border font-medium disabled:opacity-50 transition-all cursor-pointer hover:brightness-125"
          style={{
            borderColor: "rgba(34, 197, 94, 0.4)",
            background: "rgba(34, 197, 94, 0.08)",
            color: "var(--success)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {status.loading === "grant" ? (
            "Granting..."
          ) : (
            <span className="flex items-center gap-1.5">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="13 4 6 11 3 8" />
              </svg>
              Grant
            </span>
          )}
        </button>
        <button
          onClick={handleDenyClick}
          disabled={status.loading !== null}
          className="text-sm px-4 py-1.5 border font-medium disabled:opacity-50 transition-all cursor-pointer hover:brightness-125"
          style={{
            borderColor: confirmDeny ? "rgba(239, 68, 68, 0.7)" : "rgba(239, 68, 68, 0.4)",
            background: confirmDeny ? "rgba(239, 68, 68, 0.18)" : "rgba(239, 68, 68, 0.08)",
            color: "var(--danger)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {status.loading === "deny" ? (
            "Denying..."
          ) : confirmDeny ? (
            <span className="flex items-center gap-1.5">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="8" cy="8" r="6" />
                <line x1="6" y1="6" x2="10" y2="10" />
                <line x1="10" y1="6" x2="6" y2="10" />
              </svg>
              Confirm Deny
            </span>
          ) : (
            <span className="flex items-center gap-1.5">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="4" y1="4" x2="12" y2="12" />
                <line x1="12" y1="4" x2="4" y2="12" />
              </svg>
              Deny
            </span>
          )}
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

function ResolutionIcon({ resolution }: { resolution: string }) {
  if (resolution === "granted") {
    return (
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="var(--success)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="inline-block mr-1.5 -mt-px">
        <polyline points="13 4 6 11 3 8" />
      </svg>
    );
  }
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="var(--danger)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="inline-block mr-1.5 -mt-px">
      <line x1="4" y1="4" x2="12" y2="12" />
      <line x1="12" y1="4" x2="4" y2="12" />
    </svg>
  );
}

export default function GatesPage() {
  const { data: pending, isLoading: pendingLoading } = useQuery({
    queryKey: ["gates-pending"],
    queryFn: api.gatesPending,
    refetchInterval: 10_000,
  });
  const { data: recent, isLoading: recentLoading } = useQuery({
    queryKey: ["gates-recent"],
    queryFn: () => api.gatesRecent(50),
    refetchInterval: 10_000,
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

      {/* Pending Approvals Section */}
      <section>
        <div className="flex items-center gap-2 mb-4">
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
            className="text-center py-10 border border-dashed"
            style={{ borderColor: "var(--border)", borderRadius: "var(--radius-md)" }}
          >
            <div className="flex justify-center mb-3">
              <svg width="32" height="32" viewBox="0 0 32 32" fill="none" stroke="var(--success)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.6 }}>
                <circle cx="16" cy="16" r="12" />
                <polyline points="21 12 14 20 11 17" />
              </svg>
            </div>
            <p className="text-sm font-medium" style={{ color: "var(--success)" }}>All clear - no pending approvals</p>
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
                    </p>
                    <ExpiryCountdown expiresAt={p.expires_at} />
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

      {/* Divider between sections */}
      <div
        className="border-t"
        style={{ borderColor: "var(--border)" }}
      />

      {/* Resolution History Section */}
      <section>
        <div className="flex items-center gap-2 mb-4">
          <h2 className="text-lg font-semibold">Resolution History</h2>
          {recent && recent.length > 0 && (
            <span
              className="text-xs font-normal"
              style={{ color: "var(--text-tertiary)" }}
            >
              ({recent.length})
            </span>
          )}
        </div>
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
                    className="table-row-hover"
                  >
                    <td className="py-2.5 pr-3">{g.kind}</td>
                    <td className="py-2.5 pr-3">{g.agent_id}</td>
                    <td className="py-2.5 pr-3">
                      <span className="inline-flex items-center">
                        <ResolutionIcon resolution={g.resolution} />
                        <StatusBadge
                          status={g.resolution === "granted" ? "PASS" : "FAIL"}
                        />
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 font-mono text-xs">
                      {g.request_id.slice(0, 12)}...
                    </td>
                    <td className="py-2.5 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
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
