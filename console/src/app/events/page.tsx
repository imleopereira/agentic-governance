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
      <p className="text-[var(--muted)] text-xs">{label}</p>
      <div className="flex items-center gap-1.5">
        <span className="font-mono text-xs break-all">{value}</span>
        <button
          onClick={copy}
          className="shrink-0 text-[10px] px-1.5 py-0.5 rounded border border-[var(--border)] text-[var(--muted)] hover:text-[var(--accent)] hover:border-[var(--accent)] transition"
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

  // Close on Escape
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
  const model = (event.metadata as Record<string, unknown>)?.model as
    | string
    | undefined;

  return (
    <>
      {/* Overlay backdrop */}
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
      />
      {/* Slide-out panel */}
      <div className="fixed top-0 right-0 h-full w-full max-w-lg bg-[var(--bg)] border-l border-[var(--border)] z-50 overflow-y-auto shadow-2xl animate-slide-in">
        <div className="sticky top-0 bg-[var(--bg)] border-b border-[var(--border)] p-4 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Event Detail</h2>
          <button
            onClick={onClose}
            className="text-[var(--muted)] hover:text-[var(--fg)] transition text-lg leading-none"
          >
            &times;
          </button>
        </div>

        <div className="p-4 space-y-4">
          {/* event_id */}
          <CopyableField label="Event ID" value={event.event_id} />

          {/* session_id — clickable to filter */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Session ID</p>
            <button
              onClick={() => {
                onFilterSession(event.session_id);
                onClose();
              }}
              className="font-mono text-xs text-[var(--accent)] hover:underline break-all text-left"
            >
              {event.session_id}
            </button>
          </div>

          {/* agent_id */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Agent ID</p>
            <p className="text-sm">{event.agent_id}</p>
          </div>

          {/* kind */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Kind</p>
            <EventKind kind={event.kind} />
          </div>

          {/* model */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Model</p>
            <p className="text-sm font-mono">
              {model ?? <span className="text-[var(--muted)]">&mdash;</span>}
            </p>
          </div>

          {/* input_hash / output_hash */}
          <div className="grid grid-cols-1 gap-3">
            <div className="space-y-0.5">
              <p className="text-[var(--muted)] text-xs">Input Hash</p>
              <p className="font-mono text-xs break-all">
                {event.input_hash ?? <span className="text-[var(--muted)]">&mdash;</span>}
              </p>
            </div>
            <div className="space-y-0.5">
              <p className="text-[var(--muted)] text-xs">Output Hash</p>
              <p className="font-mono text-xs break-all">
                {event.output_hash ?? <span className="text-[var(--muted)]">&mdash;</span>}
              </p>
            </div>
          </div>

          {/* Full metadata */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Metadata (full)</p>
            <pre className="font-mono text-xs bg-black/30 rounded p-3 max-h-64 overflow-auto whitespace-pre-wrap">
              {JSON.stringify(event.metadata, null, 2)}
            </pre>
          </div>

          {/* prev_hash — clickable */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Prev Hash</p>
            {event.prev_hash ? (
              <button
                onClick={() => {
                  // Find event with matching hmac_value and jump to it
                  onJumpToEvent(event.prev_hash!);
                }}
                className="font-mono text-xs text-[var(--accent)] hover:underline break-all text-left"
                title="Jump to predecessor event"
              >
                {event.prev_hash}
              </button>
            ) : (
              <p className="font-mono text-xs text-[var(--muted)]">&mdash; (chain head)</p>
            )}
          </div>

          {/* HMAC */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">HMAC Value</p>
            <p className="font-mono text-xs break-all">
              {event.hmac_value ?? <span className="text-[var(--muted)]">&mdash;</span>}
            </p>
          </div>

          {/* chain_seq */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Chain Sequence</p>
            <p className="text-sm font-mono">{event.chain_seq}</p>
          </div>

          {/* created_at */}
          <div className="space-y-0.5">
            <p className="text-[var(--muted)] text-xs">Created At</p>
            <p className="text-sm font-mono">
              {event.created_at ?? <span className="text-[var(--muted)]">&mdash;</span>}
            </p>
          </div>

          {/* Verify button */}
          <div className="pt-2 border-t border-[var(--border)]">
            {verifyResult ? (
              <div className="space-y-2">
                <div
                  className={`text-xs font-medium ${
                    thisEventResult?.verified ? "text-green-400" : "text-red-400"
                  }`}
                >
                  This event:{" "}
                  {thisEventResult?.verified
                    ? "HMAC verified"
                    : "HMAC FAILED"}
                </div>
                <div
                  className={`text-xs ${
                    verifyResult.verified ? "text-green-400" : "text-red-400"
                  }`}
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
                className="w-full text-xs px-3 py-2 rounded-lg border border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)]/10 disabled:opacity-50 transition"
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
      // Find the event whose hmac_value matches the prev_hash
      const target = events?.find((e) => e.hmac_value === prevHash);
      if (target) {
        setSelectedEvent(target);
      }
    },
    [events]
  );

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
          onChange={(e) => { setAgentFilter(e.target.value); setPage(0); }}
          className="bg-[var(--card)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm w-48 focus:border-[var(--accent)] outline-none transition"
        />
        <input
          placeholder="Filter by kind"
          value={kindFilter}
          onChange={(e) => { setKindFilter(e.target.value); setPage(0); }}
          className="bg-[var(--card)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm w-48 focus:border-[var(--accent)] outline-none transition"
        />
        <input
          placeholder="Filter by session_id"
          value={sessionFilter}
          onChange={(e) => { setSessionFilter(e.target.value); setPage(0); }}
          className="bg-[var(--card)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm w-64 focus:border-[var(--accent)] outline-none transition"
        />
        {(agentFilter || kindFilter || sessionFilter) && (
          <button
            onClick={() => {
              setAgentFilter("");
              setKindFilter("");
              setSessionFilter("");
              setPage(0);
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
                <th className="py-2 pr-3">Model</th>
                <th className="py-2 pr-3">Session</th>
                <th className="py-2 pr-3">Metadata</th>
                <th className="py-2 pr-3 w-24">Time</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr
                  key={e.event_id}
                  onClick={() => setSelectedEvent(e)}
                  className="border-b border-[var(--border)] hover:bg-white/5 transition-colors cursor-pointer"
                >
                  <td className="py-2 pr-3 text-[var(--muted)] text-xs">
                    {e.chain_seq}
                  </td>
                  <td className="py-2 pr-3">
                    <EventKind kind={e.kind} />
                  </td>
                  <td className="py-2 pr-3 text-sm">{e.agent_id}</td>
                  <td className="py-2 pr-3 text-xs font-mono text-[var(--muted)]">
                    {(e.metadata as Record<string, unknown>)?.model as string ?? "\u2014"}
                  </td>
                  <td className="py-2 pr-3">
                    <button
                      onClick={(ev) => {
                        ev.stopPropagation();
                        setSessionFilter(e.session_id);
                      }}
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
          <div className="flex items-center justify-between mt-3">
            <div className="flex items-center gap-2">
              <label className="text-xs text-[var(--muted)]">Page size:</label>
              <select
                value={pageSize}
                onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}
                className="bg-[var(--card)] border border-[var(--border)] rounded px-2 py-1 text-xs focus:border-[var(--accent)] outline-none"
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
                className="text-xs px-3 py-1.5 rounded-lg border border-[var(--border)] text-[var(--muted)] hover:text-[var(--accent)] hover:border-[var(--accent)] disabled:opacity-30 transition"
              >
                Prev
              </button>
              <span className="text-xs text-[var(--muted)]">Page {page + 1}</span>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={!events || events.length < pageSize}
                className="text-xs px-3 py-1.5 rounded-lg border border-[var(--border)] text-[var(--muted)] hover:text-[var(--accent)] hover:border-[var(--accent)] disabled:opacity-30 transition"
              >
                Next
              </button>
            </div>
            <p className="text-xs text-[var(--muted)]">
              Showing {events.length} events (most recent first)
            </p>
          </div>
        </div>
      )}

      {/* Event detail slide-out panel */}
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
