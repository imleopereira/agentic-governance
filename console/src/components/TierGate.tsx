"use client";

/**
 * TierGate — wraps premium UI with a non-intrusive lock indicator when
 * the active tier doesn't include the feature. Fallback modes:
 * "locked" | "hidden" | a custom ReactNode.
 *
 * Two automatic layout modes based on `minHeight`:
 *   - **Compact** (`minHeight < 80`): renders an inline pill-button replacing
 *     the control entirely. Use case: locked inline actions (export button,
 *     halt button, reviewer-action button). The pill is the SAME SIZE as
 *     the control it replaced so layouts don't shift.
 *   - **Card** (`minHeight >= 80` OR undefined): renders the full surface
 *     with a top-right lock chip + a bottom card containing value-prop +
 *     tier dots + unlock CTA. Use case: locked dashboard panels (cost tree,
 *     SSO settings, webhook config).
 */

import { Lock, Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { useTier, type TierFeature } from "@/lib/tierContext";
import { TIER_LABELS } from "@/lib/demoUsage";

export interface TierGateProps {
  feature: TierFeature;
  children: ReactNode;
  fallback?: "locked" | "hidden" | ReactNode;
  minHeight?: number;
}

// One-line value-prop per feature — what the operator gets when they unlock.
const FEATURE_COPY: Partial<Record<TierFeature, string>> = {
  events_read: "Full audit-event visibility for the active tenant.",
  agents_basic: "Agent fleet view with per-agent status + activity.",
  agents_unlimited: "Unlimited agent registrations without tier caps.",
  cost_basic: "Per-agent spend overview across the active tenant.",
  cost_reconciliation: "Reconciled cost deltas between projected and actual spend.",
  cost_forecast: "Per-agent cap-in-N-days forecasting and spend projections.",
  gates_read: "Pending-approval queue for all active agents.",
  reviewer_actions: "Grant / deny approval tokens with audit-chained evidence.",
  multi_approver_hitl: "N-of-M approval quorum with per-request reviewer assignment.",
  webhook_config: "Outbound webhooks for cost thresholds and policy events.",
  command_palette_halt: "⌘K command palette with agent halt across the fleet.",
  compliance_read: "Compliance dashboard + coverage indicators.",
  article12_csv_unsigned: "EU AI Act Article 12 evidence export (CSV, unsigned).",
  article12_csv_signed: "HMAC-signed Article 12 evidence bundles auditors can verify.",
  sso_scim: "Single sign-on, SCIM provisioning, and per-group access policies.",
  stream: "Live audit-event stream with filter rails and pinning.",
  dedicated_csm: "Named customer success manager with 4-hour response SLA.",
};

function featureCopy(feature: TierFeature): string {
  return FEATURE_COPY[feature] ?? "Unlocks premium governance capabilities.";
}

export function TierGate({ feature, children, fallback = "locked", minHeight }: TierGateProps) {
  const { tier, hasFeature, tierMinimum, pulseUpgradeTarget } = useTier();

  if (hasFeature(feature)) return <>{children}</>;
  if (fallback === "hidden") return null;
  if (fallback !== "locked") return <>{fallback}</>;

  const required = tierMinimum(feature);
  const tiers: Array<"starter" | "team" | "enterprise"> = ["starter", "team", "enterprise"];

  // Compact mode: the locked control is an inline button / small UI — replace
  // the children with a same-size pill-button. Avoids overlaying a bottom
  // card on a 40px-tall export button (which looked broken).
  const isCompact = typeof minHeight === "number" && minHeight < 80;
  if (isCompact) {
    return (
      <button
        type="button"
        onClick={() => pulseUpgradeTarget(required)}
        aria-label={`Locked — available on ${TIER_LABELS[required]} tier. Click to preview.`}
        title={featureCopy(feature)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 12px",
          minHeight,
          background: "rgba(130, 40, 245, 0.06)",
          border: "1px solid rgba(130, 40, 245, 0.25)",
          color: "var(--accent)",
          borderRadius: "var(--radius-sm)",
          fontSize: "0.8125rem",
          fontWeight: 500,
          letterSpacing: "0.01em",
          cursor: "pointer",
          transition: "background var(--transition-fast), border-color var(--transition-fast)",
          whiteSpace: "nowrap",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "rgba(130, 40, 245, 0.1)";
          e.currentTarget.style.borderColor = "rgba(130, 40, 245, 0.4)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "rgba(130, 40, 245, 0.06)";
          e.currentTarget.style.borderColor = "rgba(130, 40, 245, 0.25)";
        }}
      >
        <Lock size={12} aria-hidden="true" />
        Available on {TIER_LABELS[required]}
      </button>
    );
  }

  return (
    <div
      role="group"
      aria-label={`Locked — ${TIER_LABELS[required]} tier or above required`}
      style={{ position: "relative", minHeight }}
    >
      {/* Dimmed but recognizable content so the operator sees WHAT they unlock. */}
      <div
        aria-hidden="true"
        style={{
          opacity: 0.6,
          pointerEvents: "none",
          userSelect: "none",
        }}
      >
        {children}
      </div>

      {/* Top-right lock chip — unobtrusive, identifies the state at a glance. */}
      <div
        aria-hidden="true"
        style={{
          position: "absolute",
          top: 12,
          right: 12,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "5px 10px",
          background: "var(--elevated)",
          border: "1px solid var(--border)",
          borderRadius: 999,
          fontSize: "0.6875rem",
          fontWeight: 600,
          letterSpacing: "0.04em",
          textTransform: "uppercase",
          color: "var(--text-secondary)",
          boxShadow: "var(--shadow-card)",
        }}
      >
        <Lock size={11} />
        {TIER_LABELS[required]}
      </div>

      {/* Bottom unlock card — informational, not a blocking overlay. */}
      <div
        style={{
          position: "absolute",
          left: 16,
          right: 16,
          bottom: 16,
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "14px 18px",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius-md)",
          boxShadow: "var(--shadow-card)",
          backdropFilter: "saturate(140%) blur(6px)",
        }}
      >
        <div
          aria-hidden="true"
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 36,
            height: 36,
            flexShrink: 0,
            borderRadius: 10,
            background: "rgba(130, 40, 245, 0.08)",
            color: "var(--accent)",
          }}
        >
          <Sparkles size={16} />
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <p
            style={{
              margin: 0,
              fontSize: "0.8125rem",
              fontWeight: 600,
              color: "var(--fg)",
              letterSpacing: "-0.01em",
              lineHeight: 1.35,
            }}
          >
            {featureCopy(feature)}
          </p>
          <div
            aria-hidden="true"
            style={{
              marginTop: 6,
              display: "flex",
              alignItems: "center",
              gap: 8,
              fontSize: "0.6875rem",
              color: "var(--text-tertiary)",
              fontFamily: "var(--font-mono)",
              letterSpacing: "0.02em",
            }}
          >
            {tiers.map((t, i) => {
              const included = tiers.indexOf(t) >= tiers.indexOf(required);
              const current = t === tier;
              return (
                <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {i > 0 && <span style={{ opacity: 0.3 }}>·</span>}
                  <span
                    style={{
                      display: "inline-block",
                      width: 6,
                      height: 6,
                      borderRadius: 999,
                      background: included ? "var(--accent)" : "var(--text-tertiary)",
                      opacity: included ? (current ? 1 : 0.7) : 0.3,
                    }}
                  />
                  <span
                    style={{
                      color: current ? "var(--fg)" : "var(--text-tertiary)",
                      fontWeight: current ? 600 : 500,
                    }}
                  >
                    {TIER_LABELS[t]}
                  </span>
                </span>
              );
            })}
          </div>
        </div>

        <button
          type="button"
          onClick={() => pulseUpgradeTarget(required)}
          style={{
            flexShrink: 0,
            padding: "8px 14px",
            fontSize: "0.75rem",
            fontWeight: 600,
            color: "#fff",
            background: "var(--accent)",
            border: "1px solid var(--accent)",
            borderRadius: "var(--radius-sm)",
            cursor: "pointer",
            whiteSpace: "nowrap",
            transition: "background var(--transition-fast)",
          }}
        >
          Unlock with {TIER_LABELS[required]}
        </button>
      </div>
    </div>
  );
}
