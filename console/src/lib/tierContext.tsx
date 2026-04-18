"use client";

/**
 * TierContext — dev-only tier simulator. Components read `tier` and
 * `hasFeature()` to decide whether to show premium UI or a lock. The
 * active tier persists to localStorage.
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";
import type { TierName } from "./demoUsage";

export type TierFeature =
  | "events_read" | "agents_basic" | "cost_basic" | "gates_read"
  | "compliance_read" | "stream"
  | "reviewer_actions" | "cost_reconciliation" | "cost_forecast"
  | "webhook_config" | "command_palette_halt" | "article12_csv_unsigned"
  | "article12_csv_signed" | "sso_scim" | "multi_approver_hitl"
  | "dedicated_csm" | "agents_unlimited";

const STARTER: TierFeature[] = [
  "events_read", "agents_basic", "cost_basic", "gates_read",
  "compliance_read", "stream",
];
const TEAM_EXTRAS: TierFeature[] = [
  "reviewer_actions", "cost_reconciliation", "cost_forecast",
  "webhook_config", "command_palette_halt", "article12_csv_unsigned",
];
const ENT_EXTRAS: TierFeature[] = [
  "article12_csv_signed", "sso_scim", "multi_approver_hitl",
  "dedicated_csm", "agents_unlimited",
];

const TIER_FEATURES: Record<TierName, TierFeature[]> = {
  starter: STARTER,
  team: [...STARTER, ...TEAM_EXTRAS],
  enterprise: [...STARTER, ...TEAM_EXTRAS, ...ENT_EXTRAS],
};

export const TIER_ORDER: TierName[] = ["starter", "team", "enterprise"];
const STORAGE_KEY = "governance_demo_tier";

interface TierContextValue {
  tier: TierName;
  setTier: (tier: TierName) => void;
  hasFeature: (feature: TierFeature) => boolean;
  tierMinimum: (feature: TierFeature) => TierName;
  pulseTarget: TierName | null;
  pulseUpgradeTarget: (tier: TierName) => void;
}

const TierCtx = createContext<TierContextValue | null>(null);

function isValidTier(v: unknown): v is TierName {
  return v === "starter" || v === "team" || v === "enterprise";
}

export function TierProvider({ children }: { children: ReactNode }) {
  const [tier, setTierState] = useState<TierName>("enterprise");
  const [pulseTarget, setPulseTarget] = useState<TierName | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (isValidTier(stored)) setTierState(stored);
    } catch { /* localStorage unavailable */ }
  }, []);

  const setTier = useCallback((next: TierName) => {
    setTierState(next);
    try { window.localStorage.setItem(STORAGE_KEY, next); } catch { /* ignore */ }
  }, []);

  const hasFeature = useCallback(
    (f: TierFeature) => TIER_FEATURES[tier].includes(f), [tier],
  );
  const tierMinimum = useCallback((f: TierFeature): TierName => {
    for (const t of TIER_ORDER) if (TIER_FEATURES[t].includes(f)) return t;
    return "enterprise";
  }, []);
  const pulseUpgradeTarget = useCallback((t: TierName) => {
    setPulseTarget(t);
    window.setTimeout(() => setPulseTarget(null), 1600);
  }, []);

  const value = useMemo<TierContextValue>(
    () => ({ tier, setTier, hasFeature, tierMinimum, pulseTarget, pulseUpgradeTarget }),
    [tier, setTier, hasFeature, tierMinimum, pulseTarget, pulseUpgradeTarget],
  );
  return <TierCtx.Provider value={value}>{children}</TierCtx.Provider>;
}

export function useTier(): TierContextValue {
  const ctx = useContext(TierCtx);
  if (!ctx) {
    // Fallback for components outside the provider: behave as Enterprise.
    return {
      tier: "enterprise",
      setTier: () => undefined,
      hasFeature: () => true,
      tierMinimum: () => "starter",
      pulseTarget: null,
      pulseUpgradeTarget: () => undefined,
    };
  }
  return ctx;
}
