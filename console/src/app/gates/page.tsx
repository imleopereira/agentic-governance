"use client";
/**
 * HITL Approval Queue (v0.5).
 * Urgency-sorted pending approvals. Approve with confirm dialog.
 * Deny requires rationale (stored in audit trail). No batch, no timers.
 */

import { useState, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type GatePending, type GateResolved } from "@/lib/api";
import { useEventStreamStore } from "@/lib/store";
import { TimeAgo } from "@/components/TimeAgo";
import { TableSkeleton, CardSkeleton } from "@/components/Skeleton";
import { CheckCircle, XCircle, AlertTriangle, Minus, Check, X } from "lucide-react";
import { TierGate } from "@/components/TierGate";

// ─── Urgency ──────────────────────────────────────────────────────────────────

type Urgency = "URGENT" | "NORMAL" | "LOW";

function getUrgency(gate: GatePending): Urgency {
  if (!gate.expires_at) return "NORMAL";
  const ms = new Date(gate.expires_at).getTime() - Date.now();
  if (ms < 5 * 60_000) return "URGENT";
  if (ms < 30 * 60_000) return "NORMAL";
  return "LOW";
}

function urgencySort(a: GatePending, b: GatePending): number {
  const order: Record<Urgency, number> = { URGENT: 0, NORMAL: 1, LOW: 2 };
  const ua = order[getUrgency(a)];
  const ub = order[getUrgency(b)];
  if (ua !== ub) return ua - ub;
  return new Date(a.created_at ?? 0).getTime() - new Date(b.created_at ?? 0).getTime();
}

const URGENCY_CONFIG: Record<Urgency, { color: string; icon: React.ElementType; label: string }> = {
  URGENT: { color: "var(--urgency-high)",   icon: AlertTriangle, label: "URGENT" },
  NORMAL: { color: "var(--urgency-medium)", icon: Minus,         label: "NORMAL" },
  LOW:    { color: "var(--urgency-low)",    icon: Check,         label: "LOW"    },
};

// ─── Approval Card ─────────────────────────────────────────────────────────────

function ApprovalCard({ gate, onResolved }: { gate: GatePending; onResolved: () => void }) {
  const urgency = getUrgency(gate);
  const { color, icon: UrgIcon, label: urgLabel } = URGENCY_CONFIG[urgency];
  const [mode, setMode] = useState<"idle" | "confirmApprove" | "denyInput">("idle");
  const [rationale, setRationale] = useState("");
  const [rationaleError, setRationaleError] = useState(false);
  const [loading, setLoading] = useState<"approve" | "deny" | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);

  const handleApprove = useCallback(async () => {
    setLoading("approve"); setApiError(null);
    try {
      await api.grantGate(gate.request_id);
      onResolved();
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Approval failed");
    } finally { setLoading(null); setMode("idle"); }
  }, [gate.request_id, onResolved]);

  const handleDeny = useCallback(async () => {
    if (!rationale.trim()) { setRationaleError(true); return; }
    setLoading("deny"); setApiError(null);
    try {
      await api.denyGate(gate.request_id, rationale.trim());
      onResolved();
    } catch (err) {
      setApiError(err instanceof Error ? err.message : "Denial failed");
    } finally { setLoading(null); setMode("idle"); }
  }, [gate.request_id, rationale, onResolved]);

  return (
    <li
      data-gate-item=""
      tabIndex={0}
      role="listitem"
      aria-label={`${urgLabel} priority: ${gate.kind} from ${gate.agent_id}`}
      style={{ background: "var(--card)", border: `1px solid ${color}25`,
        borderLeft: `3px solid ${color}`, borderRadius: "var(--radius-md)",
        padding: "1rem 1.25rem", animation: "fade-in-up 0.2s ease-out" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "0.75rem", marginBottom: "0.75rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span aria-label={`Urgency: ${urgLabel}`}
            style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "2px 7px",
              borderRadius: "var(--radius-sm)", background: `${color}15`, border: `1px solid ${color}35`,
              fontSize: "0.6875rem", fontWeight: 700, color, textTransform: "uppercase" }}>
            <UrgIcon size={10} aria-hidden="true" />{urgLabel}
          </span>
          <span className="font-mono" style={{ fontSize: "0.875rem", fontWeight: 600 }}>{gate.kind}</span>
          <span style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>from</span>
          <span className="font-mono" style={{ fontSize: "0.875rem", color: "var(--accent-light)" }}>{gate.agent_id}</span>
        </div>
        <span style={{ fontSize: "0.75rem", color: "var(--text-tertiary)", flexShrink: 0 }}>
          {gate.created_at ? <TimeAgo iso={gate.created_at} /> : "—"}
        </span>
      </div>

      {/* Request info */}
      <p style={{ fontSize: "0.75rem", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", marginBottom: "0.875rem" }}>
        ID: {gate.request_id.slice(0, 20)}…
        {gate.expires_at && (
          <span style={{ marginLeft: 12, color: urgency === "URGENT" ? "var(--danger)" : "var(--text-tertiary)" }}>
            expires <TimeAgo iso={gate.expires_at} />
          </span>
        )}
      </p>

      {/* Deny rationale input */}
      {mode === "denyInput" && (
        <div style={{ marginBottom: "0.875rem" }}>
          <label htmlFor={`rationale-${gate.request_id}`}
            style={{ display: "block", fontSize: "0.75rem", color: "var(--text-secondary)", marginBottom: 4 }}>
            Denial rationale <span style={{ color: "var(--danger)" }}>*</span>
          </label>
          <textarea
            id={`rationale-${gate.request_id}`}
            value={rationale}
            onChange={(e) => { setRationale(e.target.value); setRationaleError(false); }}
            placeholder="Why is this request being denied? (required, stored in audit trail)"
            aria-required="true"
            aria-invalid={rationaleError}
            aria-describedby={rationaleError ? `rationale-err-${gate.request_id}` : undefined}
            rows={3}
            style={{ width: "100%", background: "var(--elevated)",
              border: `1px solid ${rationaleError ? "var(--danger)" : "var(--border)"}`,
              borderRadius: "var(--radius-sm)", color: "var(--fg)", padding: "0.5rem",
              fontSize: "0.875rem", resize: "vertical", outline: "none",
              fontFamily: "var(--font-sans)" }}
          />
          {rationaleError && (
            <p id={`rationale-err-${gate.request_id}`}
              style={{ fontSize: "0.75rem", color: "var(--danger)", marginTop: 3 }}>
              Rationale is required to deny a request.
            </p>
          )}
        </div>
      )}

      {/* Action buttons */}
      <TierGate feature="reviewer_actions" minHeight={40}>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", alignItems: "center" }}>
        {mode === "confirmApprove" ? (
          <>
            <span style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>Approve {gate.kind}?</span>
            <button
              onClick={handleApprove}
              disabled={loading !== null}
              aria-label={`Confirm approve ${gate.kind} from ${gate.agent_id}`}
              style={{ padding: "0.375rem 0.875rem", fontSize: "0.8125rem",
                background: "var(--approve-bg)", border: "1px solid var(--approve-border)",
                color: "var(--success)", borderRadius: "var(--radius-sm)", cursor: "pointer",
                fontWeight: 500 }}>
              {loading === "approve" ? "Approving…" : "Confirm Approve"}
            </button>
            <button onClick={() => setMode("idle")}
              style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem", background: "none",
                border: "1px solid var(--border)", color: "var(--text-secondary)",
                borderRadius: "var(--radius-sm)", cursor: "pointer" }}>Cancel</button>
          </>
        ) : mode === "denyInput" ? (
          <>
            <button
              onClick={handleDeny}
              disabled={loading !== null}
              aria-label={`Confirm deny ${gate.kind} from ${gate.agent_id}`}
              style={{ padding: "0.375rem 0.875rem", fontSize: "0.8125rem",
                background: "var(--deny-bg)", border: "1px solid var(--deny-border)",
                color: "var(--danger)", borderRadius: "var(--radius-sm)", cursor: "pointer",
                fontWeight: 500 }}>
              {loading === "deny" ? "Denying…" : "Confirm Deny"}
            </button>
            <button onClick={() => { setMode("idle"); setRationale(""); setRationaleError(false); }}
              style={{ padding: "0.375rem 0.75rem", fontSize: "0.8125rem", background: "none",
                border: "1px solid var(--border)", color: "var(--text-secondary)",
                borderRadius: "var(--radius-sm)", cursor: "pointer" }}>Cancel</button>
          </>
        ) : (
          <>
            <button
              onClick={() => setMode("confirmApprove")}
              aria-label={`Approve ${gate.kind} request from ${gate.agent_id}`}
              aria-keyshortcuts="a"
              style={{ display: "flex", alignItems: "center", gap: 5,
                padding: "0.375rem 0.875rem", fontSize: "0.8125rem",
                background: "var(--approve-bg)", border: "1px solid var(--approve-border)",
                color: "var(--success)", borderRadius: "var(--radius-sm)", cursor: "pointer", fontWeight: 500 }}>
              <Check size={13} aria-hidden="true" /> Approve
            </button>
            <button
              onClick={() => setMode("denyInput")}
              aria-label={`Deny ${gate.kind} request from ${gate.agent_id}`}
              aria-keyshortcuts="r"
              style={{ display: "flex", alignItems: "center", gap: 5,
                padding: "0.375rem 0.875rem", fontSize: "0.8125rem",
                background: "var(--deny-bg)", border: "1px solid var(--deny-border)",
                color: "var(--danger)", borderRadius: "var(--radius-sm)", cursor: "pointer", fontWeight: 500 }}>
              <X size={13} aria-hidden="true" /> Deny
            </button>
          </>
        )}
        {apiError && <span style={{ fontSize: "0.75rem", color: "var(--danger)" }}>{apiError}</span>}
      </div>
      </TierGate>
      <TierGate feature="multi_approver_hitl" fallback="hidden">
        <p
          style={{
            marginTop: 8,
            fontSize: "0.6875rem",
            color: "var(--accent-gold)",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <span aria-hidden="true">◆</span> Multi-approver required (2 of 3)
        </p>
      </TierGate>
    </li>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function GatesPage() {
  const setPendingApprovals = useEventStreamStore((s) => s.setPendingApprovals);
  const qc = useQueryClient();

  // v0.6.2 followup: pending is now a GatesPendingPage; accumulate pages
  // client-side on Load-More. Initial page is the top-500 by created_at
  // DESC (matches the server's default LIMIT + ORDER BY).
  const [extraPages, setExtraPages] = useState<GatePending[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const { data: pending, isLoading: pendingLoading } = useQuery({
    queryKey: ["gates-pending"],
    queryFn: () => api.gatesPending(),
    refetchInterval: 5_000,
  });

  // Sync cursor state from the first page — refetch can flip has_more
  // from true→false (backlog drained) or false→true (new burst arrived).
  useEffect(() => {
    setNextCursor(pending?.has_more ? pending.next_cursor : null);
  }, [pending?.has_more, pending?.next_cursor]);

  const { data: recent, isLoading: recentLoading } = useQuery({
    queryKey: ["gates-recent"],
    queryFn: () => api.gatesRecent(50),
    refetchInterval: 10_000,
  });

  const items = pending?.items ?? [];
  const allPending = extraPages.length > 0 ? [...items, ...extraPages] : items;

  // Keep sidebar badge in sync with the FIRST page count — Load-More
  // accumulations are a display concern, not a "needs attention" count.
  useEffect(() => {
    setPendingApprovals(items.length);
    return () => setPendingApprovals(0);
  }, [items.length, setPendingApprovals]);

  // Update tab title with pending count
  useEffect(() => {
    const count = items.length;
    document.title = count > 0
      ? `(${count}) Approval Queue — Governance`
      : "Approval Queue — Governance";
    return () => { document.title = "Governance Console — Code Atelier"; };
  }, [items.length]);

  const sorted = allPending.slice().sort(urgencySort);

  async function loadMore() {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.gatesPending(nextCursor);
      setExtraPages((prev) => [...prev, ...page.items]);
      setNextCursor(page.has_more ? page.next_cursor : null);
    } catch (err) {
      // Non-fatal: Load-More is an escape-hatch. Show in console; the
      // 5s refetch of the first page will resync state.
      console.error("load more failed", err);
    } finally {
      setLoadingMore(false);
    }
  }

  async function invalidate() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["gates-pending"] }),
      qc.invalidateQueries({ queryKey: ["gates-recent"] }),
    ]);
  }

  // Keyboard: J/K navigation handled at list level
  function handleListKey(e: React.KeyboardEvent<HTMLUListElement>) {
    const items = e.currentTarget.querySelectorAll<HTMLElement>("[data-gate-item]");
    const focused = e.currentTarget.querySelector<HTMLElement>("[data-gate-item]:focus-within");
    const idx = focused ? Array.from(items).indexOf(focused) : -1;
    if (e.key === "j" || e.key === "J") {
      e.preventDefault();
      items[Math.min(items.length - 1, idx + 1)]?.focus();
    } else if (e.key === "k" || e.key === "K") {
      e.preventDefault();
      items[Math.max(0, idx - 1)]?.focus();
    }
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <h1 style={{ fontSize: "1.375rem", fontWeight: 700, marginBottom: "0.25rem" }}>
            Approval Queue
            {items.length > 0 && (
              <span aria-label={
                pending?.has_more
                  ? `${items.length}+ pending (more available)`
                  : `${items.length} pending`
              }
                title={pending?.has_more ? "500+ pending — click Load More to paginate" : undefined}
                style={{ marginLeft: 10, background: "var(--danger)", color: "#fff",
                  fontSize: "0.75rem", fontWeight: 700, padding: "2px 8px",
                  borderRadius: 10, verticalAlign: "middle" }}>
                {items.length}{pending?.has_more ? "+" : ""}
              </span>
            )}
          </h1>
          <p style={{ fontSize: "0.875rem", color: "var(--text-tertiary)" }}>
            Human-in-the-loop approvals pending agent action.
          </p>
        </div>
      </div>

      {/* Pending list */}
      <section aria-label="Pending approvals">
        {pendingLoading ? (
          <div className="space-y-2">
            <CardSkeleton /><CardSkeleton />
          </div>
        ) : sorted.length === 0 ? (
          <div style={{ textAlign: "center", padding: "3rem 2rem",
            border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}
            role="status" aria-label="No pending approvals">
            <CheckCircle size={36} style={{ color: "var(--success)", opacity: 0.7, margin: "0 auto 0.75rem" }} aria-hidden="true" />
            <p style={{ fontWeight: 500, color: "var(--success)", marginBottom: "0.25rem" }}>No pending approvals</p>
            <p style={{ fontSize: "0.8125rem", color: "var(--text-tertiary)" }}>
              Agents requesting HITL approval will appear here.
            </p>
          </div>
        ) : (
          <>
            <ul role="list" aria-label={`${sorted.length} pending approval${sorted.length !== 1 ? "s" : ""}`}
              onKeyDown={handleListKey}
              style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: "0.75rem" }}>
              {sorted.map((gate) => (
                <ApprovalCard key={gate.request_id} gate={gate} onResolved={invalidate} />
              ))}
            </ul>
            {nextCursor && (
              <div style={{ display: "flex", justifyContent: "center", marginTop: "1rem" }}>
                <button
                  type="button"
                  onClick={loadMore}
                  disabled={loadingMore}
                  aria-label="Load more pending approvals"
                  style={{
                    padding: "0.5rem 1rem", fontSize: "0.8125rem",
                    border: "1px solid var(--border)", borderRadius: "var(--radius-sm)",
                    background: "var(--surface)", color: "var(--text-primary)",
                    cursor: loadingMore ? "wait" : "pointer",
                    opacity: loadingMore ? 0.6 : 1,
                  }}
                >
                  {loadingMore ? "Loading…" : "Load more"}
                </button>
              </div>
            )}
          </>
        )}
      </section>

      {/* Divider */}
      <div style={{ borderTop: "1px solid var(--border)" }} />

      {/* Resolution history */}
      <section aria-label="Resolution history">
        <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, marginBottom: "0.875rem" }}>
          Resolution History
          {recent && recent.length > 0 && (
            <span style={{ marginLeft: 8, fontSize: "0.8125rem", color: "var(--text-tertiary)", fontWeight: 400 }}>
              ({recent.length})
            </span>
          )}
        </h2>
        {recentLoading ? (
          <TableSkeleton rows={5} />
        ) : !recent || recent.length === 0 ? (
          <div style={{ textAlign: "center", padding: "2rem",
            border: "1px dashed var(--border)", borderRadius: "var(--radius-md)" }}>
            <p style={{ fontSize: "0.875rem", color: "var(--text-tertiary)" }}>No approvals resolved yet.</p>
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", fontSize: "0.875rem", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-tertiary)" }}>
                  {["Kind","Agent","Resolution","Request ID","Resolved","Rationale"].map((h) => (
                    <th key={h} scope="col"
                      style={{ padding: "0.5rem 0.75rem 0.5rem 0", textAlign: "left",
                        fontSize: "0.6875rem", fontWeight: 500, textTransform: "uppercase",
                        letterSpacing: "0.04em", whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {recent.map((g) => (
                  <tr key={g.request_id} className="table-row-hover">
                    <td style={{ padding: "0.625rem 0.75rem 0.625rem 0" }}>{g.kind}</td>
                    <td style={{ padding: "0.625rem 0.75rem 0.625rem 0" }}
                      className="font-mono">{g.agent_id}</td>
                    <td style={{ padding: "0.625rem 0.75rem 0.625rem 0" }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 5,
                        color: g.resolution === "granted" ? "var(--success)" : "var(--danger)" }}>
                        {g.resolution === "granted"
                          ? <CheckCircle size={13} aria-hidden="true" />
                          : <XCircle size={13} aria-hidden="true" />}
                        {g.resolution}
                      </span>
                    </td>
                    <td className="font-mono" style={{ padding: "0.625rem 0.75rem 0.625rem 0", fontSize: "0.75rem", color: "var(--text-tertiary)" }}>
                      {g.request_id.slice(0, 12)}…
                    </td>
                    <td style={{ padding: "0.625rem 0.75rem 0.625rem 0",
                      fontSize: "0.8125rem", color: "var(--text-tertiary)" }}>
                      <TimeAgo iso={g.resolved_at} />
                    </td>
                    <td style={{ padding: "0.625rem 0 0.625rem 0",
                      fontSize: "0.75rem", color: "var(--text-tertiary)", maxWidth: 200 }}>
                      {g.rationale ? (
                        <span title={g.rationale} style={{ display: "block", overflow: "hidden",
                          textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{g.rationale}</span>
                      ) : "—"}
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
