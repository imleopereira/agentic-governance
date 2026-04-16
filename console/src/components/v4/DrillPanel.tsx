"use client";

/**
 * DrillPanel — non-modal slide-over aside for a single agent.
 *
 * v0.6 F1 Console Honesty Pass changes:
 *  - `role="complementary"` + `<aside>` — this is NOT a modal dialog, it's
 *    a persistent side panel next to the agents list. The previous
 *    `role="dialog" aria-modal="false"` was a lie to AT (WCAG 1.3.1).
 *  - Focus trap removed. Non-modal drawers must not trap tab — focus
 *    should flow naturally back to the list (WCAG 2.1.2 No Keyboard Trap,
 *    WCAG 2.4.3 Focus Order).
 *  - Halt button removed. The disabled "Halt (v0.6)" button had no
 *    accessible explanation for the disabled state (WCAG 4.1.2). The
 *    halt action ships via F2.5 as a separate wire call.
 *  - Contracts tab removed from `TABS`. The ContractsPanel file stays
 *    on disk (not deleted) — it's just unmounted from the drawer.
 *  - Metric strip shows non-duplicative data: last event timestamp,
 *    integration mode, chain status. The previous strip duplicated
 *    columns already shown in AgentsList (WCAG 3.2.4 Consistent
 *    Identification — don't say the same thing twice with different
 *    phrasing).
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { TrailPanel } from "./drill/TrailPanel";
import { ScopePanel } from "./drill/ScopePanel";
import { BudgetPanel } from "./drill/BudgetPanel";
import { SessionsPanel } from "./drill/SessionsPanel";
import { StatusDot } from "./StatusDot";
import { useAgent, useAgentTrail } from "@/hooks/useAgentQueries";
import { mapAgentStatus } from "@/lib/v4/statusMap";
import type { AuditEvent } from "@/lib/api";

export type DrillTabId = "trail" | "scope" | "budget" | "sessions";

export interface DrillPanelProps {
  agentId: string;
  open: boolean;
  onClose: () => void;
  /** Element to restore focus to on close. Usually the list row trigger. */
  triggerRef?: RefObject<HTMLElement | null>;
  /** Optional initial tab. Defaults to "trail". */
  initialTab?: DrillTabId;
}

/** Exported so F1 DrillPanel.test can assert length === 4 (no Contracts). */
export const TABS: ReadonlyArray<{ id: DrillTabId; label: string }> = [
  { id: "trail", label: "Trail" },
  { id: "scope", label: "Scope" },
  { id: "budget", label: "Budget" },
  { id: "sessions", label: "Sessions" },
];

function formatEventTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(11, 19);
}

function inferIntegrationMode(event: AuditEvent | undefined): string {
  if (!event) return "—";
  const kind = event.kind;
  if (kind.startsWith("llm.")) return "wrap_llm";
  if (kind.startsWith("chain.")) return "langchain";
  if (kind.startsWith("routing.")) return "routing";
  if (kind.startsWith("tool.")) return "tool";
  return "manual";
}

function inferChainStatus(event: AuditEvent | undefined): {
  label: string;
  ok: boolean;
} {
  if (!event) return { label: "—", ok: true };
  if (event.kind === "chain.degraded_start")
    return { label: "degraded", ok: false };
  return event.hmac_value
    ? { label: "verified", ok: true }
    : { label: "unverified", ok: false };
}

export function DrillPanel({
  agentId,
  open,
  onClose,
  triggerRef,
  initialTab = "trail",
}: DrillPanelProps) {
  const [tab, setTab] = useState<DrillTabId>(initialTab);
  const asideRef = useRef<HTMLElement | null>(null);
  const { data: agent } = useAgent(open ? agentId : null);
  const { data: trail } = useAgentTrail(open ? agentId : null, 1);

  const latestEvent = trail?.[0];
  const integrationMode = useMemo(
    () => inferIntegrationMode(latestEvent),
    [latestEvent],
  );
  const chainStatus = useMemo(
    () => inferChainStatus(latestEvent),
    [latestEvent],
  );
  const lastEventTs = useMemo(
    () => formatEventTime(latestEvent?.created_at),
    [latestEvent],
  );

  // On open, move focus into the aside so keyboard users land inside
  // the newly-rendered panel. On close, restore focus to the trigger.
  // NO focus trap — Tab flows back to the list naturally.
  useEffect(() => {
    if (!open) return;
    const aside = asideRef.current;
    if (!aside) return;
    const first = aside.querySelector<HTMLElement>(
      'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (first) first.focus();
    else aside.focus();
    const restoreTarget = triggerRef?.current ?? null;
    return () => {
      if (restoreTarget && typeof restoreTarget.focus === "function") {
        restoreTarget.focus();
      }
    };
  }, [open, triggerRef]);

  // Escape closes. NO Tab cycling — this is a non-modal drawer.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    },
    [onClose],
  );

  if (!open) return null;

  return (
    <aside
      ref={asideRef}
      role="complementary"
      aria-label={agentId}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="flex h-full w-full flex-col overflow-y-auto outline-none"
      style={{
        background: "var(--bg)",
      }}
    >
      {/* Header */}
      <div
        className="sticky top-0 z-30 flex items-start justify-between px-5 py-4 border-b"
        style={{
          background: "var(--bg)",
          borderColor: "var(--border)",
        }}
      >
        <div className="flex flex-col gap-1 min-w-0">
          <h2
            className="font-mono text-sm font-semibold truncate"
            style={{ color: "var(--fg)" }}
          >
            {agentId}
          </h2>
          {agent && <StatusDot status={mapAgentStatus(agent)} />}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close drill panel"
            className="rounded-md border px-3 py-1 text-xs"
            style={{
              borderColor: "var(--border)",
              color: "var(--fg)",
            }}
          >
            Close
          </button>
        </div>
      </div>

      {/* Metric strip — non-duplicative of AgentsList columns. */}
      <div
        className="grid grid-cols-3 px-5 py-3 border-b"
        style={{ borderColor: "var(--border)", gap: 12 }}
      >
        {[
          { label: "Last event", value: lastEventTs, danger: false },
          { label: "Integration", value: integrationMode, danger: false },
          {
            label: "Chain",
            value: chainStatus.label,
            danger: !chainStatus.ok,
          },
        ].map((m) => (
          <div key={m.label}>
            <div
              className="font-mono uppercase"
              style={{
                fontSize: 9,
                letterSpacing: "0.1em",
                color: "var(--text-tertiary)",
              }}
            >
              {m.label}
            </div>
            <div
              className="font-mono"
              style={{
                fontSize: 12,
                marginTop: 2,
                color: m.danger ? "var(--danger)" : "var(--fg)",
              }}
            >
              {m.value}
            </div>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <nav
        role="tablist"
        aria-label="Agent detail tabs"
        className="flex gap-1 px-5 border-b"
        style={{ borderColor: "var(--border)" }}
      >
        {TABS.map((t) => {
          const active = t.id === tab;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={active}
              aria-controls={`drill-tabpanel-${t.id}`}
              id={`drill-tab-${t.id}`}
              onClick={() => setTab(t.id)}
              className="font-sans"
              style={{
                padding: "7px 10px",
                fontSize: 11,
                fontWeight: 500,
                color: active ? "var(--fg)" : "var(--text-tertiary)",
                borderBottom: active
                  ? "2px solid var(--fg)"
                  : "2px solid transparent",
                marginBottom: -1,
                background: "transparent",
              }}
            >
              {t.label}
            </button>
          );
        })}
      </nav>

      {/* Tab content */}
      <div
        role="tabpanel"
        id={`drill-tabpanel-${tab}`}
        aria-labelledby={`drill-tab-${tab}`}
        className="p-5"
      >
        {tab === "trail" && <TrailPanel agentId={agentId} />}
        {tab === "scope" && <ScopePanel agentId={agentId} />}
        {tab === "budget" && <BudgetPanel agentId={agentId} />}
        {tab === "sessions" && <SessionsPanel agentId={agentId} />}
      </div>
    </aside>
  );
}
