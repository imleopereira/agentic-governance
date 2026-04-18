"use client";

/**
 * TierGate — wraps premium UI with a lock overlay when the active tier
 * doesn't include the feature. Fallback modes: "locked" | "hidden" |
 * a custom ReactNode.
 */

import { Lock } from "lucide-react";
import type { ReactNode } from "react";
import { useTier, type TierFeature } from "@/lib/tierContext";
import { TIER_LABELS } from "@/lib/demoUsage";

export interface TierGateProps {
  feature: TierFeature;
  children: ReactNode;
  fallback?: "locked" | "hidden" | ReactNode;
  minHeight?: number;
}

export function TierGate({ feature, children, fallback = "locked", minHeight }: TierGateProps) {
  const { hasFeature, tierMinimum, pulseUpgradeTarget } = useTier();

  if (hasFeature(feature)) return <>{children}</>;
  if (fallback === "hidden") return null;
  if (fallback !== "locked") return <>{fallback}</>;

  const required = tierMinimum(feature);

  return (
    <div
      role="group"
      aria-label={`Locked — ${TIER_LABELS[required]} tier required`}
      style={{ position: "relative", minHeight }}
    >
      <div aria-hidden="true" style={{
        opacity: 0.35, filter: "grayscale(0.6)",
        pointerEvents: "none", userSelect: "none",
      }}>{children}</div>
      <div style={{
        position: "absolute", inset: 0,
        display: "flex", alignItems: "center", justifyContent: "center",
        background: "linear-gradient(180deg, rgba(13,15,20,0.55), rgba(13,15,20,0.75))",
        borderRadius: "var(--radius-md)", backdropFilter: "blur(1px)",
      }}>
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center", gap: 10,
          padding: "12px 16px", background: "var(--surface)",
          border: "1px solid var(--border)", borderRadius: "var(--radius-md)",
          boxShadow: "var(--shadow-card)",
          maxWidth: "min(90%, 340px)", textAlign: "center",
        }}>
          <div aria-hidden="true" style={{
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            width: 28, height: 28, borderRadius: 999,
            background: "var(--elevated)", border: "1px solid var(--border)",
            color: "var(--text-secondary)",
          }}><Lock size={14} /></div>
          <p style={{
            fontSize: "0.8125rem", fontWeight: 500,
            color: "var(--fg)", lineHeight: 1.4,
          }}>Available on {TIER_LABELS[required]} and above</p>
          <button
            type="button"
            onClick={() => pulseUpgradeTarget(required)}
            style={{
              padding: "6px 14px", fontSize: "0.75rem", fontWeight: 600,
              background: "var(--accent)", border: "1px solid var(--accent)",
              color: "#fff", borderRadius: "var(--radius-sm)", cursor: "pointer",
            }}
          >Upgrade to {TIER_LABELS[required]}</button>
        </div>
      </div>
    </div>
  );
}
