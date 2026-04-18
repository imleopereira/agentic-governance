"use client";

/**
 * /admin/sso — Enterprise-only placeholder. v0.8 ships the real config;
 * this page exists so the Enterprise demo has a destination.
 */

import { KeyRound, Users as UsersIcon, FileSignature } from "lucide-react";
import { TierGate } from "@/components/TierGate";

const SECTIONS: Array<{ icon: React.ElementType; title: string; description: string; value: string }> = [
  { icon: KeyRound, title: "Single sign-on (SAML 2.0)",
    description: "Delegate login to Okta, Azure AD, or Google Workspace with JIT provisioning.", value: "Ready" },
  { icon: UsersIcon, title: "SCIM 2.0 provisioning",
    description: "Auto-provision agents and reviewers from your directory with role mapping.", value: "Ready" },
  { icon: FileSignature, title: "Business Associate Agreement",
    description: "BAA covers PHI in audit metadata for HIPAA workloads. Request via your CSM.", value: "Available" },
];

export default function AdminSsoPage() {
  return (
    <TierGate feature="sso_scim" minHeight={320}>
      <div className="space-y-6">
        <div>
          <p style={{
            fontSize: "0.6875rem", textTransform: "uppercase",
            letterSpacing: "0.08em", color: "var(--text-tertiary)", marginBottom: 4,
          }}>Enterprise</p>
          <h1 className="text-2xl font-bold tracking-tight mb-1">Identity &amp; access</h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            SSO, SCIM, and BAA controls. Preview — full config ships in v0.8.
          </p>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
          {SECTIONS.map(({ icon: Icon, title, description, value }) => (
            <div key={title} style={{
              display: "flex", alignItems: "flex-start", gap: 14, padding: 16,
              border: "1px solid var(--border)", borderRadius: "var(--radius-md)",
              background: "var(--card)",
            }}>
              <div aria-hidden="true" style={{
                width: 32, height: 32, borderRadius: "var(--radius-sm)",
                background: "var(--elevated)", display: "inline-flex",
                alignItems: "center", justifyContent: "center",
                color: "var(--accent-gold)", flexShrink: 0,
              }}><Icon size={16} /></div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                  <p style={{ fontSize: "0.9375rem", fontWeight: 600 }}>{title}</p>
                  <span style={{
                    fontSize: "0.6875rem", padding: "2px 8px",
                    background: "rgba(242, 185, 75, 0.12)",
                    border: "1px solid rgba(242, 185, 75, 0.35)",
                    color: "var(--accent-gold)", borderRadius: 999,
                    fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em",
                  }}>{value}</span>
                </div>
                <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>{description}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </TierGate>
  );
}
