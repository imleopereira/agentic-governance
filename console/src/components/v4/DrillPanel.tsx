"use client";

/**
 * DrillPanel — slide-over dialog with focus trap and tab navigation.
 *
 * PRD §6.2:
 *  - role="dialog" + aria-modal="true"
 *  - Focus trap: Tab/Shift-Tab cycle within panel
 *  - Escape closes
 *  - Focus restoration via triggerRef on close
 *  - Tabs: Trail / Scope / Budget / Sessions / Contracts (NO Delegation)
 *  - Halt button disabled with v0.6 tooltip
 *
 * The focus-trap is ~40 lines, hand-rolled (no focus-trap-react dep).
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import { TrailPanel } from "./drill/TrailPanel";
import { ScopePanel } from "./drill/ScopePanel";
import { BudgetPanel } from "./drill/BudgetPanel";
import { SessionsPanel } from "./drill/SessionsPanel";
import { ContractsPanel } from "./ContractsPanel";
import { StatusDot } from "./StatusDot";
import { useAgent } from "@/hooks/useAgentQueries";
import { mapAgentStatus } from "@/lib/v4/statusMap";

export type DrillTabId =
  | "trail"
  | "scope"
  | "budget"
  | "sessions"
  | "contracts";

export interface DrillPanelProps {
  agentId: string;
  open: boolean;
  onClose: () => void;
  /** Element to restore focus to on close. Usually the list row trigger. */
  triggerRef?: RefObject<HTMLElement | null>;
  /** Optional initial tab. Defaults to "trail". */
  initialTab?: DrillTabId;
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

const TABS: ReadonlyArray<{ id: DrillTabId; label: string }> = [
  { id: "trail", label: "Trail" },
  { id: "scope", label: "Scope" },
  { id: "budget", label: "Budget" },
  { id: "sessions", label: "Sessions" },
  { id: "contracts", label: "Contracts" },
];

const HALT_TOOLTIP =
  "Halt API pending — requires operator token model, self-approval prevention, and signed audit write. Target: v0.6.";

export function DrillPanel({
  agentId,
  open,
  onClose,
  triggerRef,
  initialTab = "trail",
}: DrillPanelProps) {
  const [tab, setTab] = useState<DrillTabId>(initialTab);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const { data: agent } = useAgent(open ? agentId : null);

  // Focus management: on open, move focus inside; on close, restore to trigger.
  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const focusables = dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    const first = focusables[0];
    if (first) {
      first.focus();
    } else {
      dialog.focus();
    }

    const restoreTarget = triggerRef?.current ?? null;
    return () => {
      if (restoreTarget && typeof restoreTarget.focus === "function") {
        restoreTarget.focus();
      }
    };
    // DA M1: include `tab` so when the operator switches tabs the
    // focusable-element list is requeried and focus re-enters the new
    // tabpanel content. Without this, focus stays on stale DOM nodes
    // from the previously-mounted tab.
  }, [open, tab, triggerRef]);

  // Keydown handler: Escape closes, Tab cycles focus within dialog.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusables = Array.from(
        dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((el) => !el.hasAttribute("disabled"));
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  if (!open) return null;

  // FIX 1: DrillPanel is no longer a fullscreen modal. It is a normal
  // flex child that fills its parent aside (rendered by the parallel
  // `@drill` slot in `(v4)/agents/layout.tsx`). No backdrop, no
  // position:fixed — the list stays visible to the left. `role="dialog"`
  // is retained for semantic parity with the previous slide-over; the
  // route itself is the "open" state, so there is no backdrop click
  // handler either. Close via the explicit button or the Escape key.
  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="false"
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
        className="sticky top-0 z-10 flex items-start justify-between px-5 py-4 border-b"
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
          {/* FIX 7: surface live status next to the agent id. */}
          {agent && <StatusDot status={mapAgentStatus(agent)} />}
        </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled
              title={HALT_TOOLTIP}
              aria-label={HALT_TOOLTIP}
              className="rounded-md border px-3 py-1 text-xs cursor-not-allowed opacity-50"
              style={{
                borderColor: "var(--border)",
                color: "var(--text-tertiary)",
              }}
            >
              Halt (v0.6)
            </button>
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

        {/* Metric strip — real PostureAgent fields only */}
        <div
          className="grid grid-cols-4 px-5 py-3 border-b"
          style={{ borderColor: "var(--border)", gap: 12 }}
        >
          {[
            {
              label: "Spend today",
              value: agent ? `$${agent.cost.usd_today.toFixed(2)}` : "—",
              danger: (agent?.cost.exceeded_today ?? 0) > 0,
            },
            {
              label: "Events total",
              value: agent
                ? agent.audit.events_total.toLocaleString()
                : "—",
              danger: false,
            },
            {
              label: "Violations today",
              value: agent ? String(agent.scope.violations_today) : "—",
              danger: (agent?.scope.violations_today ?? 0) > 0,
            },
            {
              label: "Pending approvals",
              value: agent ? String(agent.gates.pending) : "—",
              danger: false,
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
          {tab === "contracts" && <ContractsPanel agentId={agentId} />}
      </div>
    </div>
  );
}
