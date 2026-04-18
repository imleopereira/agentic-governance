"use client";

/**
 * TierSwitcher — floating dev-only tier selector. Bottom-right on
 * desktop, bottom-sheet FAB on mobile. Renders only in dev or with
 * `?demo=1`.
 */

import { useEffect, useState } from "react";
import { useTier, TIER_ORDER } from "@/lib/tierContext";
import { TIER_LABELS, TIER_PRICES, type TierName } from "@/lib/demoUsage";

const TIER_COLOR: Record<TierName, string> = {
  starter: "var(--text-secondary)",
  team: "var(--accent-violet)",
  enterprise: "var(--accent-gold)",
};

function useShouldRender(): boolean {
  const [ok, setOk] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const dev = process.env.NODE_ENV === "development";
    const forced = new URLSearchParams(window.location.search).get("demo") === "1";
    setOk(dev || forced);
  }, []);
  return ok;
}

function useIsMobile(): boolean {
  const [m, setM] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 639px)");
    const u = () => setM(mq.matches);
    u();
    mq.addEventListener("change", u);
    return () => mq.removeEventListener("change", u);
  }, []);
  return m;
}

function TierRow({ onPick }: { onPick?: () => void }) {
  const { tier, setTier, pulseTarget } = useTier();
  return (
    <div
      role="group"
      aria-label="Demo tier selector"
      style={{
        display: "inline-flex", background: "var(--elevated)",
        border: "1px solid var(--border)", borderRadius: 999, padding: 3, gap: 2,
        boxShadow: "var(--shadow-elevated)",
      }}
    >
      {TIER_ORDER.map((t) => {
        const active = t === tier;
        return (
          <button
            key={t}
            type="button"
            aria-pressed={active}
            onClick={() => { setTier(t); onPick?.(); }}
            className={pulseTarget === t ? "tier-pulse" : undefined}
            style={{
              padding: "6px 12px", fontSize: "0.75rem",
              fontWeight: active ? 600 : 500, lineHeight: 1, borderRadius: 999,
              border: "1px solid transparent",
              background: active ? "var(--bg)" : "transparent",
              color: active ? TIER_COLOR[t] : "var(--text-secondary)",
              cursor: "pointer", letterSpacing: "0.01em",
              transition: "background var(--transition-fast), color var(--transition-fast)",
            }}
          >
            {TIER_LABELS[t]}
            <span aria-hidden="true" style={{
              marginLeft: 6, fontSize: "0.625rem",
              color: "var(--text-tertiary)", fontFamily: "var(--font-mono)",
            }}>
              {TIER_PRICES[t]}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function TierSwitcher() {
  const show = useShouldRender();
  const isMobile = useIsMobile();
  const { tier } = useTier();
  const [sheetOpen, setSheetOpen] = useState(false);

  if (!show) return null;

  if (isMobile) {
    return (
      <>
        <button
          type="button"
          aria-label="Open demo tier selector"
          aria-expanded={sheetOpen}
          onClick={() => setSheetOpen((s) => !s)}
          style={{
            position: "fixed", bottom: 16, right: 16, zIndex: 60,
            padding: "8px 12px", background: "var(--elevated)",
            border: "1px solid var(--border)", borderRadius: 999,
            color: TIER_COLOR[tier], fontSize: "0.75rem", fontWeight: 600,
            boxShadow: "var(--shadow-elevated)", cursor: "pointer",
          }}
        >
          Tier: {TIER_LABELS[tier]}
        </button>
        {sheetOpen ? (
          <div
            role="dialog" aria-label="Demo tier selector"
            style={{
              position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 70,
              padding: "16px 16px 24px", background: "var(--surface)",
              borderTop: "1px solid var(--border)",
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            <p style={{
              fontSize: "0.6875rem", textTransform: "uppercase",
              letterSpacing: "0.06em", color: "var(--text-tertiary)", marginBottom: 8,
            }}>
              Demo tier selector (dev only)
            </p>
            <TierRow onPick={() => setSheetOpen(false)} />
          </div>
        ) : null}
      </>
    );
  }

  return (
    <div style={{
      position: "fixed", bottom: 16, right: 16, zIndex: 60,
      display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4,
    }}>
      <span style={{
        fontSize: "0.625rem", textTransform: "uppercase",
        letterSpacing: "0.08em", color: "var(--text-tertiary)",
        fontFamily: "var(--font-mono)",
      }}>
        Demo tier selector (dev only)
      </span>
      <TierRow />
    </div>
  );
}
