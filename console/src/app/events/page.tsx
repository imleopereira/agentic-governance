"use client";

import { useState, useEffect, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type AuditEvent, type VerifyResult } from "@/lib/api";
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
        className="text-xs font-medium"
        style={{ color: result.verified ? "var(--success)" : "var(--danger)" }}
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
      className="text-xs px-3 py-1.5 border transition-colors disabled:opacity-50"
      style={{
        borderColor: "var(--accent)",
        color: "var(--accent)",
        borderRadius: "var(--radius-sm)",
      }}
    >
      {loading ? "Verifying..." : "Verify chain integrity"}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Copyable field helper
// ---------------------------------------------------------------------------
function CopyableField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="space-y-0.5">
      <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>{label}</p>
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-xs break-all">{value}</span>
        <button
          onClick={copy}
          className="shrink-0 text-[10px] px-1.5 py-0.5 border transition-colors"
          style={{
            borderColor: copied ? "var(--accent)" : "var(--border)",
            color: copied ? "var(--accent)" : "var(--text-tertiary)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Event detail slide-out panel
// ---------------------------------------------------------------------------
function EventDetailPanel({
  event,
  onClose,
  onFilterSession,
  onJumpToEvent,
}: {
  event: AuditEvent;
  onClose: () => void;
  onFilterSession: (sessionId: string) => void;
  onJumpToEvent: (eventId: string) => void;
}) {
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  async function verifyEvent() {
    setVerifying(true);
    try {
      const r = await api.verifySession(event.session_id);
      setVerifyResult(r);
    } catch {
      setVerifyResult(null);
    } finally {
      setVerifying(false);
    }
  }

  const thisEventResult = verifyResult?.events.find(
    (e) => e.event_id === event.event_id
  );
  const model = event.model ?? (event.metadata as Record<string, unknown>)?.model as string | undefined;

  return (
    <>
      <div className="fixed inset-0 bg-black/50 z-40" onClick={onClose} />
      <div
        className="fixed top-0 right-0 h-full w-full max-w-lg border-l z-50 overflow-y-auto shadow-2xl animate-slide-in"
        style={{ background: "var(--bg)", borderColor: "var(--border)" }}
      >
        <div
          className="sticky top-0 border-b p-4 flex items-center justify-between"
          style={{ background: "var(--bg)", borderColor: "var(--border)" }}
        >
          <h2 className="text-sm font-semibold">Event Detail</h2>
          <button
            onClick={onClose}
            className="transition text-lg leading-none"
            style={{ color: "var(--text-tertiary)" }}
          >
            &times;
          </button>
        </div>

        <div className="p-4 space-y-4">
          <CopyableField label="Event ID" value={event.event_id} />

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Session ID</p>
            <button
              onClick={() => {
                onFilterSession(event.session_id);
                onClose();
              }}
              className="font-mono text-xs hover:underline break-all text-left"
              style={{ color: "var(--accent)" }}
            >
              {event.session_id}
            </button>
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Agent ID</p>
            <p className="text-sm">{event.agent_id}</p>
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Kind</p>
            <EventKind kind={event.kind} />
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Model</p>
            <p className="text-sm font-mono">
              {model ?? <span style={{ color: "var(--text-tertiary)" }}>&mdash;</span>}
            </p>
          </div>

          <div className="grid grid-cols-1 gap-3">
            <div className="space-y-0.5">
              <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Input Hash</p>
              <p className="font-mono text-xs break-all">
                {event.input_hash ?? <span style={{ color: "var(--text-tertiary)" }}>&mdash;</span>}
              </p>
            </div>
            <div className="space-y-0.5">
              <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Output Hash</p>
              <p className="font-mono text-xs break-all">
                {event.output_hash ?? <span style={{ color: "var(--text-tertiary)" }}>&mdash;</span>}
              </p>
            </div>
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Metadata (full)</p>
            <pre
              className="font-mono text-xs p-3 max-h-64 overflow-auto whitespace-pre-wrap"
              style={{
                background: "rgba(0, 0, 0, 0.3)",
                borderRadius: "var(--radius-sm)",
              }}
            >
              {JSON.stringify(event.metadata, null, 2)}
            </pre>
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Prev Hash</p>
            {event.prev_hash ? (
              <button
                onClick={() => onJumpToEvent(event.prev_hash!)}
                className="font-mono text-xs hover:underline break-all text-left"
                style={{ color: "var(--accent)" }}
                title="Jump to predecessor event"
              >
                {event.prev_hash}
              </button>
            ) : (
              <p className="font-mono text-xs" style={{ color: "var(--text-tertiary)" }}>
                &mdash; (chain head)
              </p>
            )}
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>HMAC Value</p>
            <p className="font-mono text-xs break-all">
              {event.hmac_value ?? <span style={{ color: "var(--text-tertiary)" }}>&mdash;</span>}
            </p>
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Chain Sequence</p>
            <p className="text-sm font-mono">{event.chain_seq}</p>
          </div>

          <div className="space-y-0.5">
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>Created At</p>
            <p className="text-sm font-mono">
              {event.created_at ?? <span style={{ color: "var(--text-tertiary)" }}>&mdash;</span>}
            </p>
          </div>

          <div className="pt-2 border-t" style={{ borderColor: "var(--border)" }}>
            {verifyResult ? (
              <div className="space-y-2">
                <div
                  className="text-xs font-medium"
                  style={{ color: thisEventResult?.verified ? "var(--success)" : "var(--danger)" }}
                >
                  This event:{" "}
                  {thisEventResult?.verified ? "HMAC verified" : "HMAC FAILED"}
                </div>
                <div
                  className="text-xs"
                  style={{ color: verifyResult.verified ? "var(--success)" : "var(--danger)" }}
                >
                  Session chain:{" "}
                  {verifyResult.verified
                    ? `All ${verifyResult.event_count} events verified`
                    : `TAMPERED at ${verifyResult.first_failure?.slice(0, 12)}...`}
                </div>
              </div>
            ) : (
              <button
                onClick={verifyEvent}
                disabled={verifying}
                className="w-full text-xs px-3 py-2 border transition-colors disabled:opacity-50"
                style={{
                  borderColor: "var(--accent)",
                  color: "var(--accent)",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                {verifying ? "Verifying..." : "Verify this event"}
              </button>
            )}
          </div>
        </div>
      </div>
    </>
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
  const [selectedEvent, setSelectedEvent] = useState<AuditEvent | null>(null);
  const [pageSize, setPageSize] = useState(50);
  const [page, setPage] = useState(0);

  const { data: events, isLoading } = useQuery({
    queryKey: ["events", agentFilter, kindFilter, sessionFilter, pageSize, page],
    queryFn: () =>
      api.events({
        agent_id: agentFilter || undefined,
        kind: kindFilter || undefined,
        session_id: sessionFilter || undefined,
        limit: pageSize,
        offset: page * pageSize,
      }),
  });

  const handleClose = useCallback(() => setSelectedEvent(null), []);
  const handleFilterSession = useCallback((sid: string) => {
    setSessionFilter(sid);
  }, []);
  const handleJumpToEvent = useCallback(
    (prevHash: string) => {
      const target = events?.find((e) => e.hmac_value === prevHash);
      if (target) setSelectedEvent(target);
    },
    [events]
  );

  const inputStyle = {
    background: "var(--card)",
    borderColor: "var(--border)",
    borderRadius: "var(--radius-sm)",
    color: "var(--fg)",
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">Audit Log Explorer</h1>
        <LiveBadge />
      </div>

      <div className="flex flex-wrap gap-3 text-sm">
        <input
          placeholder="Filter by agent_id"
          value={agentFilter}
          onChange={(e) => { setAgentFilter(e.target.value); setPage(0); }}
          className="border px-3 py-2 text-sm w-48 outline-none transition-colors focus:border-[var(--accent)]"
          style={inputStyle}
        />
        <input
          placeholder="Filter by kind"
          value={kindFilter}
          onChange={(e) => { setKindFilter(e.target.value); setPage(0); }}
          className="border px-3 py-2 text-sm w-48 outline-none transition-colors focus:border-[var(--accent)]"
          style={inputStyle}
        />
        <input
          placeholder="Filter by session_id"
          value={sessionFilter}
          onChange={(e) => { setSessionFilter(e.target.value); setPage(0); }}
          className="border px-3 py-2 text-sm w-64 outline-none transition-colors focus:border-[var(--accent)]"
          style={inputStyle}
        />
        {(agentFilter || kindFilter || sessionFilter) && (
          <button
            onClick={() => {
              setAgentFilter("");
              setKindFilter("");
              setSessionFilter("");
              setPage(0);
            }}
            className="text-xs transition-colors hover:text-white"
            style={{ color: "var(--text-tertiary)" }}
          >
            Clear filters
          </button>
        )}
      </div>

      {sessionFilter && (
        <div
          className="flex items-center gap-3 p-3 border"
          style={{
            background: "var(--card)",
            borderColor: "var(--border)",
            borderRadius: "var(--radius-md)",
          }}
        >
          <span className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            Session {sessionFilter.slice(0, 8)}...:
          </span>
          <VerifyButton sessionId={sessionFilter} />
        </div>
      )}

      {isLoading ? (
        <TableSkeleton rows={10} />
      ) : !events || events.length === 0 ? (
        <div className="text-center py-12">
          <p style={{ color: "var(--text-tertiary)" }}>
            {agentFilter || kindFilter || sessionFilter
              ? "No events match your filters."
              : "No audit events yet. Start logging from the SDK."}
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left border-b" style={{ borderColor: "var(--border)", color: "var(--text-tertiary)" }}>
                <th className="py-2 pr-3 w-12 font-medium text-xs uppercase tracking-wider">#</th>
                <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Kind</th>
                <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Agent</th>
                <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Model</th>
                <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Session</th>
                <th className="py-2 pr-3 font-medium text-xs uppercase tracking-wider">Metadata</th>
                <th className="py-2 pr-3 w-24 font-medium text-xs uppercase tracking-wider">Time</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr
                  key={e.event_id}
                  onClick={() => setSelectedEvent(e)}
                  className="border-b hover:bg-white/[0.03] transition-colors cursor-pointer"
                  style={{ borderColor: "var(--border)" }}
                >
                  <td className="py-2 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
                    {e.chain_seq}
                  </td>
                  <td className="py-2 pr-3">
                    <EventKind kind={e.kind} />
                  </td>
                  <td className="py-2 pr-3 text-sm">{e.agent_id}</td>
                  <td className="py-2 pr-3 text-xs font-mono" style={{ color: "var(--text-tertiary)" }}>
                    {e.model ?? (e.metadata as Record<string, unknown>)?.model as string ?? "\u2014"}
                  </td>
                  <td className="py-2 pr-3">
                    <button
                      onClick={(ev) => {
                        ev.stopPropagation();
                        setSessionFilter(e.session_id);
                      }}
                      className="hover:underline font-mono text-xs"
                      style={{ color: "var(--accent)" }}
                    >
                      {e.session_id.slice(0, 8)}...
                    </button>
                  </td>
                  <td className="py-2 pr-3 max-w-xs">
                    <MetadataViewer data={e.metadata as Record<string, unknown>} />
                  </td>
                  <td className="py-2 pr-3 text-xs" style={{ color: "var(--text-tertiary)" }}>
                    <TimeAgo iso={e.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex items-center justify-between mt-3">
            <div className="flex items-center gap-2">
              <label className="text-xs" style={{ color: "var(--text-tertiary)" }}>Page size:</label>
              <select
                value={pageSize}
                onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}
                className="border px-2 py-1 text-xs outline-none"
                style={inputStyle}
              >
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
              </select>
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="text-xs px-3 py-1.5 border disabled:opacity-30 transition-colors"
                style={{
                  borderColor: "var(--border)",
                  color: "var(--text-tertiary)",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                Prev
              </button>
              <span className="text-xs" style={{ color: "var(--text-tertiary)" }}>Page {page + 1}</span>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={!events || events.length < pageSize}
                className="text-xs px-3 py-1.5 border disabled:opacity-30 transition-colors"
                style={{
                  borderColor: "var(--border)",
                  color: "var(--text-tertiary)",
                  borderRadius: "var(--radius-sm)",
                }}
              >
                Next
              </button>
            </div>
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
              Showing {events.length} events (most recent first)
            </p>
          </div>
        </div>
      )}

      {selectedEvent && (
        <EventDetailPanel
          event={selectedEvent}
          onClose={handleClose}
          onFilterSession={handleFilterSession}
          onJumpToEvent={handleJumpToEvent}
        />
      )}
    </div>
  );
}
