"use client";
/**
 * Live Event Stream - SSE-driven, virtual-scrolled audit event feed.
 * Data flows: SSE -> Zustand store -> filtered view.
 * Historical events loaded on demand from REST API.
 */

import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type AuditEvent } from "@/lib/api";
import { useEventStreamStore, type StreamEvent } from "@/lib/store";
import { VirtualList } from "@/components/VirtualList";
import { TimeAgo } from "@/components/TimeAgo";
import { Activity, Pause, Play, ChevronDown, CheckCircle } from "lucide-react";

// ─── Smart-batching types ─────────────────────────────────────────────────────

interface BatchGroup {
  _type: "batch";
  id: string;
  events: StreamEvent[];
  count: number;
}

type DisplayItem = StreamEvent | BatchGroup;

function isBatch(item: DisplayItem): item is BatchGroup {
  return "_type" in item;
}

const BATCH_WINDOW_MS = 200;
const BATCH_THRESHOLD = 10;

/** Collapse rapid-fire bursts of SSE events into a single summary row. */
function applySmartBatching(
  events: StreamEvent[],
  expandedBatchIds: ReadonlySet<string>
): DisplayItem[] {
  const result: DisplayItem[] = [];
  let i = 0;
  while (i < events.length) {
    const current = events[i];
    if (current._received_at <= 0) { result.push(current); i++; continue; }
    let j = i + 1;
    while (
      j < events.length &&
      events[j]._received_at > 0 &&
      events[i]._received_at - events[j]._received_at < BATCH_WINDOW_MS
    ) { j++; }
    const batchSize = j - i;
    const batchId = `batch-${current._local_id}`;
    if (batchSize > BATCH_THRESHOLD && !expandedBatchIds.has(batchId)) {
      result.push({ _type: "batch", id: batchId, events: events.slice(i, j), count: batchSize });
    } else {
      for (let k = i; k < j; k++) result.push(events[k]);
    }
    i = j;
  }
  return result;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const KIND_COLOR: Record<string, string> = {
  audit:    "var(--event-audit)",
  scope:    "var(--event-scope)",
  budget:   "var(--event-budget)",
  hitl:     "var(--event-hitl)",
  loop:     "var(--event-loop)",
  system:   "var(--event-system)",
  presence: "var(--status-idle)",
  gate:     "var(--event-hitl)",
};

function kindColor(kind: string): string {
  return KIND_COLOR[kind.toLowerCase()] ?? "var(--event-audit)";
}

function kindLabel(kind: string): string {
  return kind.toUpperCase().slice(0, 6).padEnd(6);
}

type Severity = "high" | "medium" | "low";

function getSeverity(kind: string): Severity {
  const k = kind.toLowerCase();
  if (k === "scope" || k === "hitl" || k === "gate") return "high";
  if (k === "budget") return "medium";
  return "low";
}

function formatTimestamp(iso: string): string {
  // UTC everywhere — DB stores UTC, console renders UTC so operators in
  // different timezones see the same wall-clock as their chain data.
  const d = new Date(iso);
  const hms = d.toISOString().slice(11, 19);
  const ms = String(d.getUTCMilliseconds()).padStart(3, "0");
  return `${hms}.${ms}`;
}

// ─── Event Row ────────────────────────────────────────────────────────────────

interface EventRowProps {
  event: StreamEvent;
  expanded: boolean;
  onToggle: () => void;
  onVerify: (sessionId: string) => void;
  onFilterAgent: (agentId: string) => void;
  onFilterKind: (kind: string) => void;
}

function EventRow({ event, expanded, onToggle, onVerify, onFilterAgent, onFilterKind }: EventRowProps) {
  const color = kindColor(event.kind);
  const ts = formatTimestamp(event.created_at);

  return (
    <article
      role="article"
      aria-label={`${ts}, ${event.kind} event, agent ${event.agent_id}`}
      aria-expanded={expanded}
      style={{ borderBottom: "1px solid var(--border)" }}
    >
      {/* Main row */}
      <div
        onClick={onToggle}
        onKeyDown={(e) => { if (e.key === "Enter") onToggle(); }}
        tabIndex={0}
        style={{
          display: "grid", gridTemplateColumns: "120px 80px 1fr auto",
          alignItems: "center", gap: "0.75rem", padding: "0.5rem 1rem",
          cursor: "pointer", borderLeft: `3px solid ${color}`,
          background: expanded ? "rgba(255,255,255,0.03)" : "transparent",
          transition: "background var(--transition-fast)",
          outline: "none",
        }}
        onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(255,255,255,0.03)"; }}
        onMouseLeave={(e) => { if (!expanded) e.currentTarget.style.background = "transparent"; }}
      >
        <span className="font-mono" style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)", flexShrink: 0 }}>
          {ts}
        </span>
        <span
          aria-label={`Event kind: ${event.kind}`}
          style={{
            fontSize: "0.6875rem", fontWeight: 600, color, fontFamily: "var(--font-mono)",
            padding: "1px 5px", background: `${color}14`, borderRadius: "var(--radius-sm)", flexShrink: 0,
          }}
        >
          {kindLabel(event.kind)}
        </span>
        <div style={{ overflow: "hidden" }}>
          <button
            onClick={(e) => { e.stopPropagation(); onFilterAgent(event.agent_id); }}
            aria-label={`Filter by agent ${event.agent_id}`}
            className="font-mono"
            style={{ fontSize: "0.8125rem", color: "var(--fg)", fontWeight: 500,
              background: "none", border: "none", cursor: "pointer", padding: 0,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {event.agent_id}
          </button>
          {event.metadata && Object.keys(event.metadata).length > 0 && (
            <p style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)", marginTop: 1,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {JSON.stringify(event.metadata).slice(0, 80)}
            </p>
          )}
        </div>
        <ChevronDown size={12} aria-hidden="true"
          style={{ color: "var(--text-tertiary)", transition: "transform var(--transition-fast)",
            transform: expanded ? "rotate(180deg)" : "rotate(0deg)", flexShrink: 0 }} />
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div role="region" aria-label="Event details"
          style={{ padding: "0.75rem 1rem 0.75rem 1.75rem", borderTop: "1px solid var(--border)",
            background: "rgba(0,0,0,0.15)" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: "0.5rem 1.5rem", marginBottom: "0.75rem" }}>
            <FieldRow label="Event ID" value={event.event_id} mono />
            {event.session_id && <FieldRow label="Session" value={event.session_id} mono />}
            {event.chain_seq !== undefined && <FieldRow label="Chain seq" value={String(event.chain_seq)} mono />}
            {event.prev_hash && <FieldRow label="Prev hash" value={event.prev_hash.slice(0, 16) + "…"} mono />}
            {event.model && <FieldRow label="Model" value={event.model} />}
          </div>
          {Object.keys(event.metadata).length > 0 && (
            <div>
              <p style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)", marginBottom: 4 }}>METADATA</p>
              <pre className="font-mono" style={{ fontSize: "0.75rem", color: "var(--fg)",
                background: "rgba(0,0,0,0.2)", padding: "0.5rem", borderRadius: "var(--radius-sm)",
                overflow: "auto", maxHeight: 120, margin: 0 }}>
                {JSON.stringify(event.metadata, null, 2)}
              </pre>
            </div>
          )}
          <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.75rem" }}>
            {event.session_id && (
              <button
                onClick={() => onVerify(event.session_id!)}
                aria-label="Verify HMAC chain for this session"
                style={{ fontSize: "0.75rem", padding: "0.25rem 0.625rem",
                  background: "rgba(130,40,245,0.08)", border: "1px solid rgba(130,40,245,0.25)",
                  color: "var(--accent-light)", borderRadius: "var(--radius-sm)", cursor: "pointer" }}>
                Verify Chain
              </button>
            )}
            <button
              onClick={() => { navigator.clipboard.writeText(event.event_id).catch(() => null); }}
              aria-label="Copy event ID"
              style={{ fontSize: "0.75rem", padding: "0.25rem 0.625rem",
                background: "none", border: "1px solid var(--border)",
                color: "var(--text-tertiary)", borderRadius: "var(--radius-sm)", cursor: "pointer" }}>
              Copy ID
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onFilterKind(event.kind); }}
              aria-label={`Filter by ${event.kind} events`}
              style={{ fontSize: "0.75rem", padding: "0.25rem 0.625rem",
                background: "none", border: "1px solid var(--border)",
                color: "var(--text-tertiary)", borderRadius: "var(--radius-sm)", cursor: "pointer" }}>
              Filter kind
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

function FieldRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <p style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)", marginBottom: 1 }}>{label}</p>
      <p className={mono ? "font-mono" : ""} style={{ fontSize: "0.75rem", color: "var(--fg)",
        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</p>
    </div>
  );
}

// ─── Verify toast ─────────────────────────────────────────────────────────────

function VerifyToast({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["verify", sessionId],
    queryFn: () => api.verifySession(sessionId),
    staleTime: 30_000,
  });
  return (
    <div role="status" aria-live="polite"
      style={{ position: "fixed", bottom: 24, right: 24, zIndex: 50,
        background: "var(--elevated)", border: "1px solid var(--border)",
        borderRadius: "var(--radius-md)", padding: "0.875rem 1rem",
        boxShadow: "var(--shadow-elevated)", maxWidth: 320, animation: "fade-in-up 0.2s ease-out" }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 }}>
        <div style={{ flex: 1 }}>
          <p style={{ fontSize: "0.8125rem", fontWeight: 500, marginBottom: 4 }}>Chain Verification</p>
          <p className="font-mono" style={{ fontSize: "0.6875rem", color: "var(--text-tertiary)" }}>
            {sessionId.slice(0, 16)}…
          </p>
          {isLoading && <p style={{ fontSize: "0.75rem", color: "var(--text-secondary)", marginTop: 6 }}>Verifying…</p>}
          {error && <p style={{ fontSize: "0.75rem", color: "var(--danger)", marginTop: 6 }}>
            {error instanceof Error ? error.message : "Verification failed"}
          </p>}
          {data && (
            <p style={{ fontSize: "0.75rem", marginTop: 6,
              color: data.verified ? "var(--success)" : "var(--danger)",
              display: "flex", alignItems: "center", gap: 4 }}>
              {data.verified ? <CheckCircle size={13} aria-hidden="true" /> : null}
              {data.verified ? `Chain intact (${data.event_count} events)` : `Broken at: ${data.first_failure ?? "unknown"}`}
            </p>
          )}
        </div>
        <button onClick={onClose} aria-label="Close verify result"
          style={{ background: "none", border: "none", cursor: "pointer",
            color: "var(--text-tertiary)", padding: 2, flexShrink: 0 }}>✕</button>
      </div>
    </div>
  );
}

// ─── Waiting skeleton ─────────────────────────────────────────────────────────

function WaitingForEvents() {
  return (
    <div style={{ padding: "3rem 1.5rem", textAlign: "center" }}
      role="status" aria-label="Waiting for events">
      <div style={{ display: "flex", justifyContent: "center", gap: 6, marginBottom: "1rem" }}>
        {[0,1,2].map((i) => (
          <div key={i} style={{
            width: 8, height: 8, borderRadius: "50%",
            background: "var(--accent)", opacity: 0.4,
            animation: `live-pulse 1.4s ease-in-out ${i * 0.2}s infinite`,
          }} aria-hidden="true" />
        ))}
      </div>
      <p style={{ fontSize: "0.875rem", fontWeight: 500, marginBottom: "0.5rem" }}>Waiting for events…</p>
      <p style={{ fontSize: "0.8125rem", color: "var(--text-tertiary)" }}>
        Events will appear here as agents send them.
      </p>
    </div>
  );
}

// ─── Batch summary row ────────────────────────────────────────────────────────

function BatchSummaryRow({ count, onExpand }: { count: number; onExpand: () => void }) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onExpand}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onExpand(); } }}
      aria-label={`${count} events in rapid succession. Activate to expand.`}
      style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        height: 72, borderBottom: "1px solid var(--border)", cursor: "pointer",
        gap: "0.5rem", background: "rgba(130,40,245,0.03)",
        transition: "background var(--transition-fast)", outline: "none",
      }}
      onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(130,40,245,0.07)"; }}
      onMouseLeave={(e) => { e.currentTarget.style.background = "rgba(130,40,245,0.03)"; }}
    >
      <span style={{ fontSize: "0.8125rem", color: "var(--text-tertiary)" }}>—</span>
      <span style={{ fontSize: "0.8125rem", fontWeight: 500, color: "var(--accent-light)" }}>
        {count} events
      </span>
      <span style={{ fontSize: "0.8125rem", color: "var(--text-tertiary)" }}>in 200ms</span>
      <span style={{
        fontSize: "0.75rem", padding: "2px 7px", borderRadius: "var(--radius-sm)",
        background: "rgba(130,40,245,0.1)", border: "1px solid rgba(130,40,245,0.2)",
        color: "var(--accent-light)",
      }}>
        click to expand
      </span>
      <span style={{ fontSize: "0.8125rem", color: "var(--text-tertiary)" }}>—</span>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function StreamPage() {
  const { events: sseEvents, connectionStatus } = useEventStreamStore();
  const [paused, setPaused] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [verifySessionId, setVerifySessionId] = useState<string | null>(null);
  const [newEventCount, setNewEventCount] = useState(0);
  const [isAtTop, setIsAtTop] = useState(true);
  const [scrollToTopTrigger, setScrollToTopTrigger] = useState(0);
  const [expandedBatchIds, setExpandedBatchIds] = useState<ReadonlySet<string>>(new Set());
  const pauseBufferRef = useRef<typeof sseEvents>([]);
  const prevSseLen = useRef(sseEvents.length);

  // Filters
  const [filterAgent, setFilterAgent] = useState("");
  const [filterKind, setFilterKind] = useState("");
  const [filterSession, setFilterSession] = useState("");
  const [filterSeverity, setFilterSeverity] = useState<"" | Severity>("");

  // REST history
  const qc = useQueryClient();
  const [histOffset, setHistOffset] = useState<number | null>(null);
  const histQuery = useQuery({
    queryKey: ["events-hist", filterAgent, filterKind, filterSession, histOffset],
    queryFn: () => api.events({
      agent_id: filterAgent || undefined, kind: filterKind || undefined,
      session_id: filterSession || undefined, limit: 50, offset: histOffset ?? 0,
    }),
    enabled: histOffset !== null,
    staleTime: 30_000,
  });
  const [histEvents, setHistEvents] = useState<AuditEvent[]>([]);
  useEffect(() => {
    if (histQuery.data) {
      setHistEvents((prev) => [...prev, ...histQuery.data!]);
    }
  }, [histQuery.data]);

  // Detect new events while scrolled down
  useEffect(() => {
    const newLen = sseEvents.length;
    if (!isAtTop && newLen > prevSseLen.current) {
      setNewEventCount((c) => c + (newLen - prevSseLen.current));
    }
    prevSseLen.current = newLen;
  }, [sseEvents.length, isAtTop]);

  // Filtered SSE events
  const filteredSse = useMemo(() => {
    return sseEvents.filter((e) => {
      if (filterAgent && !e.agent_id.toLowerCase().includes(filterAgent.toLowerCase())) return false;
      if (filterKind && e.kind !== filterKind) return false;
      if (filterSession && e.session_id !== filterSession) return false;
      if (filterSeverity && getSeverity(e.kind) !== filterSeverity) return false;
      return true;
    });
  }, [sseEvents, filterAgent, filterKind, filterSession, filterSeverity]);

  // Convert hist events to StreamEvent format for unified display
  const histAsStream = useMemo((): typeof sseEvents => {
    return histEvents.map((e, i) => ({
      event_id: e.event_id, session_id: e.session_id, agent_id: e.agent_id,
      kind: e.kind, model: e.model, metadata: e.metadata,
      created_at: e.created_at ?? new Date().toISOString(),
      chain_seq: e.chain_seq, hmac_value: e.hmac_value, prev_hash: e.prev_hash,
      _received_at: -i, _local_id: `hist-${e.event_id}`,
    }));
  }, [histEvents]);

  const allEvents = useMemo(() => {
    const seen = new Set(filteredSse.map((e) => e.event_id));
    const unique = histAsStream.filter((e) => !seen.has(e.event_id));
    return [...filteredSse, ...unique];
  }, [filteredSse, histAsStream]);

  function resetFilters() {
    setFilterAgent(""); setFilterKind(""); setFilterSession(""); setFilterSeverity("");
    setHistEvents([]); setHistOffset(null);
  }

  const isConnecting = connectionStatus === "connecting";
  const showWaiting = (isConnecting || connectionStatus === "connected") && allEvents.length === 0;

  // Apply pause filter then smart batching for the virtual list
  const displayItems: DisplayItem[] = applySmartBatching(
    paused
      ? allEvents.filter((e) => e._received_at <= (pauseBufferRef.current[0]?._received_at ?? Infinity))
      : allEvents,
    expandedBatchIds
  );
  const hasActiveFilters = filterAgent || filterKind || filterSession || filterSeverity;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: "0.75rem" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0 }}>
        <h1 style={{ fontSize: "1.375rem", fontWeight: 700 }}>Live Event Stream</h1>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.8125rem" }}>
            {/* Indicator precedence (critical → informational):
                  1. Disconnected → red, no pulse — connection loss trumps even
                     user-initiated pause, so an operator who paused and then
                     lost the connection still sees the red indicator at a glance.
                  2. Connecting   → blue, no pulse.
                  3. Paused       → amber, no pulse — user froze the feed themselves.
                  4. Connected / polling → green + pulse (default happy state). */}
            {(() => {
              const isDisconnected = connectionStatus === "disconnected";
              const isConnecting = connectionStatus === "connecting";
              const isLive =
                connectionStatus === "connected" || connectionStatus === "polling";
              const dotColor = isDisconnected
                ? "var(--danger)"
                : isConnecting
                ? "var(--status-idle)"
                : paused
                ? "var(--warn)"
                : "var(--status-live)";
              const dotAnim = isLive && !paused
                ? "live-pulse 2s ease-in-out infinite"
                : undefined;
              const labelText = isDisconnected
                ? paused ? "Disconnected — paused" : "Disconnected"
                : isConnecting
                ? "Connecting…"
                : paused
                ? "Paused"
                : "Streaming";
              const labelColor = isDisconnected
                ? "var(--danger)"
                : paused
                ? "var(--warn)"
                : "var(--text-secondary)";
              const ariaStatus = isDisconnected
                ? paused ? "disconnected and paused" : "disconnected"
                : paused
                ? "paused"
                : connectionStatus;
              return (
                <>
                  <span aria-hidden="true" style={{
                    width: 8, height: 8, borderRadius: "50%",
                    background: dotColor,
                    animation: dotAnim,
                  }} />
                  <span aria-label={`Stream status: ${ariaStatus}`} style={{ color: labelColor }}>
                    {labelText}
                  </span>
                </>
              );
            })()}
          </span>
          <button
            onClick={() => setPaused((v) => !v)}
            aria-label={paused ? "Resume streaming" : "Pause streaming"}
            aria-pressed={paused}
            aria-keyshortcuts="Space"
            style={{ display: "flex", alignItems: "center", gap: 5, padding: "0.375rem 0.75rem",
              fontSize: "0.8125rem", background: "none", border: "1px solid var(--border)",
              color: paused ? "var(--warn)" : "var(--text-secondary)",
              borderRadius: "var(--radius-sm)", cursor: "pointer" }}>
            {paused ? <Play size={12} aria-hidden="true" /> : <Pause size={12} aria-hidden="true" />}
            {paused ? "Resume" : "Pause"}
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div role="search" aria-label="Filter events"
        style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", flexShrink: 0 }}>
        <input value={filterAgent} onChange={(e) => setFilterAgent(e.target.value)}
          placeholder="Agent ID…" className="filter-input" style={{ minWidth: 140, flex: 1 }}
          aria-label="Filter by agent ID" />
        <select value={filterKind} onChange={(e) => setFilterKind(e.target.value)}
          className="filter-input" aria-label="Filter by event kind"
          style={{ minWidth: 110 }}>
          <option value="">All kinds</option>
          {["audit","scope","budget","hitl","loop","system","presence","gate"].map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
        <input value={filterSession} onChange={(e) => setFilterSession(e.target.value)}
          placeholder="Session ID…" className="filter-input" style={{ minWidth: 140, flex: 1 }}
          aria-label="Filter by session ID" />
        <select value={filterSeverity} onChange={(e) => setFilterSeverity(e.target.value as "" | Severity)}
          className="filter-input" aria-label="Filter by severity"
          style={{ minWidth: 120 }}>
          <option value="">All severity</option>
          <option value="high">HIGH — scope / HITL</option>
          <option value="medium">MEDIUM — budget</option>
          <option value="low">LOW — audit / system</option>
        </select>
        {hasActiveFilters && (
          <button onClick={resetFilters} className="btn-ghost"
            style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem" }}>Reset</button>
        )}
      </div>

      {/* New events pill */}
      {!isAtTop && newEventCount > 0 && (
        <button
          onClick={() => { setScrollToTopTrigger((t) => t + 1); setNewEventCount(0); setIsAtTop(true); }}
          aria-live="polite"
          aria-label={`${newEventCount} new events - click to scroll to top`}
          style={{ position: "fixed", top: 80, left: "50%", transform: "translateX(-50%)", zIndex: 20,
            background: "var(--accent)", color: "#fff", border: "none", borderRadius: 20,
            padding: "0.375rem 1rem", fontSize: "0.8125rem", cursor: "pointer",
            boxShadow: "0 2px 12px rgba(130,40,245,0.4)", animation: "fade-in-up 0.2s ease-out" }}>
          ↑ {newEventCount} new event{newEventCount !== 1 ? "s" : ""}
        </button>
      )}

      {/* Event list */}
      <div style={{ flex: 1, border: "1px solid var(--border)", borderRadius: "var(--radius-md)",
        overflow: "hidden", background: "var(--card)", minHeight: 0 }}
        role="log" aria-label="Live event stream" aria-live={paused ? "off" : "polite"}>
        {showWaiting ? (
          <WaitingForEvents />
        ) : allEvents.length === 0 ? (
          <div style={{ padding: "3rem", textAlign: "center" }}>
            <Activity size={32} style={{ color: "var(--text-tertiary)", margin: "0 auto 0.75rem" }} aria-hidden="true" />
            <p style={{ fontSize: "0.875rem", color: "var(--text-tertiary)" }}>No events match the current filters.</p>
          </div>
        ) : (
          <VirtualList<DisplayItem>
            items={displayItems}
            itemHeight={72}
            scrollToTopTrigger={scrollToTopTrigger}
            onScrolledToTop={() => { setIsAtTop(true); setNewEventCount(0); }}
            onScrolledToBottom={() => setIsAtTop(false)}
            renderItem={(item) => {
              if (isBatch(item)) {
                return (
                  <BatchSummaryRow
                    key={item.id}
                    count={item.count}
                    onExpand={() =>
                      setExpandedBatchIds((prev) => new Set([...prev, item.id]))
                    }
                  />
                );
              }
              return (
                <EventRow
                  key={item._local_id}
                  event={item}
                  expanded={expandedId === item._local_id}
                  onToggle={() => setExpandedId((id) => id === item._local_id ? null : item._local_id)}
                  onVerify={setVerifySessionId}
                  onFilterAgent={(id) => setFilterAgent(id)}
                  onFilterKind={(k) => setFilterKind(k)}
                />
              );
            }}
          />
        )}
      </div>

      {/* Load older */}
      <div style={{ flexShrink: 0, textAlign: "center" }}>
        <button
          onClick={() => setHistOffset(histEvents.length || 0)}
          disabled={histQuery.isFetching}
          className="btn-ghost"
          style={{ fontSize: "0.8125rem", padding: "0.375rem 1rem" }}>
          {histQuery.isFetching ? "Loading…" : "Load older events"}
        </button>
      </div>

      {/* Chain verify toast */}
      {verifySessionId && (
        <VerifyToast sessionId={verifySessionId} onClose={() => setVerifySessionId(null)} />
      )}
    </div>
  );
}
