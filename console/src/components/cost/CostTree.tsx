"use client";

/**
 * CostTree — reconciliation-delta tree for the /cost page.
 *
 * PRD §F2: three-level tree (Agent → Session → Model → last-5-calls) with
 * green/amber/red delta cells. Consumes data shaped like:
 *
 *   [
 *     {
 *       agent_id: "billing-agent",
 *       sessions: [
 *         {
 *           session_id: "…",
 *           models: [
 *             { model: "gpt-4o", governance_usd: 4.82, invoice_usd: 4.84,
 *               recent_calls: [{ ts, tokens, usd }, …] }
 *           ]
 *         }
 *       ]
 *     }
 *   ]
 *
 * For v0.7 the page feeds curated/mock data until the reconciliation
 * endpoint ships (v0.7.1 — the `CostModule.reconcile()` Postgres view is
 * v0.6.2). We render what we have; when the prop is empty we show the
 * empty state rather than a blank frame.
 *
 * Delta color bucket (delta = invoice_usd - governance_usd):
 *   |delta| ≤ 2%  → positive (green, var(--success))
 *   |delta| ≤ 5%  → warn     (amber, var(--warn))
 *   otherwise     → danger   (red,   var(--danger))
 *
 * The PRD mentioned `--status-{positive,warn,danger}` tokens; the actual
 * tokens in globals.css are `--success / --warn / --danger`. We use those
 * directly (documented mismatch, no new tokens added for a single surface).
 */

import { useState } from "react";
import { ChevronRight } from "lucide-react";

export interface CostCall {
  ts: string;
  tokens: number;
  usd: number;
}

export interface CostModel {
  model: string;
  governance_usd: number;
  invoice_usd: number;
  recent_calls?: CostCall[];
}

export interface CostSession {
  session_id: string;
  models: CostModel[];
}

export interface CostAgent {
  agent_id: string;
  sessions: CostSession[];
}

export interface CostTreeProps {
  agents: CostAgent[];
}

type DeltaClass = "positive" | "warn" | "danger";

function classifyDelta(governance: number, invoice: number): {
  amount: number;
  pct: number;
  cls: DeltaClass;
} {
  const amount = invoice - governance;
  const base = Math.max(Math.abs(governance), 0.0001);
  const pct = Math.abs(amount / base) * 100;
  let cls: DeltaClass = "positive";
  if (pct > 5) cls = "danger";
  else if (pct > 2) cls = "warn";
  return { amount, pct, cls };
}

function deltaColor(cls: DeltaClass): string {
  if (cls === "positive") return "var(--success)";
  if (cls === "warn") return "var(--warn)";
  return "var(--danger)";
}

function DeltaCell({ cls, amount }: { cls: DeltaClass; amount: number }) {
  const sign = amount >= 0 ? "+" : "−";
  return (
    <span
      aria-label={`Delta ${cls} ${sign}${Math.abs(amount).toFixed(4)} USD`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "1px 8px",
        borderRadius: 999,
        fontSize: "0.6875rem",
        fontWeight: 600,
        background: "transparent",
        border: `1px solid ${deltaColor(cls)}`,
        color: deltaColor(cls),
        fontFamily: "var(--font-mono)",
      }}
    >
      Δ {sign}${Math.abs(amount).toFixed(2)}
    </span>
  );
}

function ModelRow({ model }: { model: CostModel }) {
  const [open, setOpen] = useState(false);
  const { amount, cls } = classifyDelta(model.governance_usd, model.invoice_usd);
  const hasCalls = (model.recent_calls?.length ?? 0) > 0;
  return (
    <li style={{ marginLeft: 24, lineHeight: 1.7, listStyle: "none" }}>
      <button
        type="button"
        onClick={() => hasCalls && setOpen(!open)}
        disabled={!hasCalls}
        aria-expanded={open}
        aria-label={`Model ${model.model}`}
        style={{
          background: "transparent",
          border: "none",
          padding: 0,
          color: "inherit",
          cursor: hasCalls ? "pointer" : "default",
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          fontFamily: "var(--font-mono)",
          fontSize: "0.8125rem",
        }}
      >
        <ChevronRight
          size={12}
          aria-hidden="true"
          style={{
            transform: open ? "rotate(90deg)" : "rotate(0deg)",
            transition: "transform 120ms ease",
            opacity: hasCalls ? 1 : 0.2,
          }}
        />
        <span>{model.model}</span>
        <span style={{ color: "var(--text-tertiary)" }}>
          · gov ${model.governance_usd.toFixed(2)} · invoice $
          {model.invoice_usd.toFixed(2)} ·
        </span>
        <DeltaCell cls={cls} amount={amount} />
      </button>
      {open && hasCalls && (
        <ul style={{ listStyle: "none", padding: 0, margin: "4px 0 4px 28px" }}>
          {model.recent_calls!.slice(0, 5).map((c, i) => (
            <li
              key={i}
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: "0.75rem",
                color: "var(--text-tertiary)",
              }}
            >
              └─ {c.ts} · {c.tokens.toLocaleString()} tok · $
              {c.usd.toFixed(4)}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function SessionNode({ session }: { session: CostSession }) {
  return (
    <li style={{ marginLeft: 16, listStyle: "none", lineHeight: 1.7 }}>
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: "0.8125rem",
          color: "var(--text-secondary)",
        }}
      >
        ├─ session {session.session_id.slice(0, 12)}…
      </span>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {session.models.map((m) => (
          <ModelRow key={`${session.session_id}-${m.model}`} model={m} />
        ))}
      </ul>
    </li>
  );
}

export function CostTree({ agents }: CostTreeProps) {
  if (!agents || agents.length === 0) {
    return (
      <div
        className="border border-dashed p-4 text-center"
        style={{
          borderColor: "var(--border)",
          borderRadius: "var(--radius-md)",
        }}
      >
        <p style={{ color: "var(--text-tertiary)", fontSize: "0.8125rem" }}>
          No reconciliation data yet. Tree populates after the first
          governance-recorded + invoice-reported pair lands.
        </p>
      </div>
    );
  }
  return (
    <div
      className="border p-4"
      style={{
        borderColor: "var(--border)",
        background: "var(--card)",
        borderRadius: "var(--radius-md)",
      }}
    >
      <p
        style={{
          color: "var(--text-secondary)",
          marginBottom: 10,
          fontSize: "0.75rem",
        }}
      >
        Governance-recorded cost vs. provider invoice (today, UTC). Click a
        model to expand the last 5 calls.
      </p>
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {agents.map((a) => (
          <li
            key={a.agent_id}
            style={{ marginBottom: 8, listStyle: "none", lineHeight: 1.7 }}
          >
            <span
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: "0.8125rem",
                fontWeight: 600,
              }}
            >
              {a.agent_id}
            </span>
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {a.sessions.map((s) => (
                <SessionNode key={s.session_id} session={s} />
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default CostTree;
