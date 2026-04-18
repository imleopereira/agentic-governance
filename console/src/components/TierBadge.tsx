"use client";

/**
 * TierBadge — header pill showing current tier + usage. Click opens a
 * mini-panel with bullets and an Upgrade CTA.
 */

import { useEffect, useRef, useState } from "react";
import { useTier } from "@/lib/tierContext";
import { DEMO_USAGE, TIER_LABELS, type TierName } from "@/lib/demoUsage";

const TIER_COLOR: Record<TierName, string> = {
  starter: "var(--text-secondary)",
  team: "var(--accent-violet)",
  enterprise: "var(--accent-gold)",
};
const NEXT_TIER: Record<TierName, TierName | null> = {
  starter: "team", team: "enterprise", enterprise: null,
};
const TIER_BULLETS: Record<TierName, string[]> = {
  starter: ["500k events / month", "Up to 2 agents", "1 seat", "Read-only gates + basic cost"],
  team: ["5M events / month", "Up to 10 agents", "5 seats", "Reviewer actions + reconciliation",
    "Article 12 CSV (unsigned)", "⌘K halt palette"],
  enterprise: ["50M events / month", "Unlimited agents", "15 seats",
    "Signed Article 12 attestation", "SSO / SCIM / BAA", "Multi-approver HITL", "Dedicated CSM"],
};

const fmt = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${Math.round(n / 1_000)}k` : String(n);

export function TierBadge() {
  const { tier, pulseUpgradeTarget } = useTier();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const click = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", click);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", click);
      document.removeEventListener("keydown", key);
    };
  }, [open]);

  const u = DEMO_USAGE[tier];
  const over = u.agents_cap !== null && u.agents_used > u.agents_cap;
  const agentsLbl = u.agents_cap === null
    ? `${u.agents_used} agents · ∞`
    : `${u.agents_used} / ${u.agents_cap} agents${over ? " ⚠" : ""}`;
  const eventsLbl = `${fmt(u.events_used)} / ${fmt(u.events_cap)} events`;
  const next = NEXT_TIER[tier];

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={`Current tier: ${TIER_LABELS[tier]}. ${eventsLbl}. ${agentsLbl}.`}
        style={{
          display: "inline-flex", alignItems: "center", gap: 8,
          padding: "5px 10px", background: "var(--elevated)",
          border: `1px solid ${over ? "var(--warn)" : "var(--border)"}`,
          borderRadius: 999, color: "var(--fg)",
          fontSize: "0.75rem", fontFamily: "var(--font-mono)",
          cursor: "pointer", lineHeight: 1,
        }}
      >
        <span aria-hidden="true" style={{ width: 6, height: 6, borderRadius: 999, background: TIER_COLOR[tier] }} />
        <span style={{ color: TIER_COLOR[tier], fontWeight: 600 }}>{TIER_LABELS[tier]}</span>
        <span style={{ color: "var(--text-tertiary)" }}>·</span>
        <span style={{ color: "var(--text-secondary)" }}>{eventsLbl}</span>
        <span style={{ color: "var(--text-tertiary)" }}>·</span>
        <span style={{ color: over ? "var(--warn)" : "var(--text-secondary)" }}>{agentsLbl}</span>
      </button>
      {open ? (
        <div role="dialog" aria-label={`${TIER_LABELS[tier]} tier summary`}
          style={{
            position: "absolute", top: "calc(100% + 6px)", right: 0, width: 260,
            padding: 14, background: "var(--surface)",
            border: "1px solid var(--border)", borderRadius: "var(--radius-md)",
            boxShadow: "var(--shadow-elevated)", zIndex: 80,
          }}
        >
          <p style={{ fontSize: "0.6875rem", textTransform: "uppercase",
            letterSpacing: "0.06em", color: "var(--text-tertiary)", marginBottom: 6 }}>Your tier</p>
          <p style={{ fontSize: "0.95rem", fontWeight: 600, color: TIER_COLOR[tier], marginBottom: 10 }}>
            {TIER_LABELS[tier]}
          </p>
          <ul style={{ listStyle: "none", padding: 0, margin: 0,
            display: "flex", flexDirection: "column", gap: 4 }}>
            {TIER_BULLETS[tier].map((line) => (
              <li key={line} style={{ fontSize: "0.75rem", color: "var(--text-secondary)", lineHeight: 1.45 }}>
                · {line}
              </li>
            ))}
          </ul>
          {next ? (
            <button type="button"
              onClick={() => { setOpen(false); pulseUpgradeTarget(next); }}
              style={{
                marginTop: 12, width: "100%", padding: "7px 10px",
                background: "var(--accent)", border: "1px solid var(--accent)",
                color: "#fff", fontSize: "0.75rem", fontWeight: 600,
                borderRadius: "var(--radius-sm)", cursor: "pointer",
              }}
            >Upgrade to {TIER_LABELS[next]}</button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
