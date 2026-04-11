"use client";

import {
  Shield,
  Activity,
  Lock,
  Eye,
  CircleDollarSign,
  FileCheck,
  CheckCircle2,
} from "lucide-react";

const FEATURE_ICONS = [
  { icon: Shield, label: "Scope enforcement" },
  { icon: CircleDollarSign, label: "Budget gates" },
  { icon: Lock, label: "Approval gates" },
  { icon: Activity, label: "Audit trail" },
  { icon: Eye, label: "Provenance" },
  { icon: FileCheck, label: "Prompt versioning" },
];

export function GettingStarted() {
  return (
    <div className="max-w-2xl mx-auto py-16 space-y-10">
      {/* Hero */}
      <div className="text-center space-y-4">
        <div
          className="inline-flex items-center justify-center w-16 h-16 mb-2"
          style={{
            background: "rgba(130, 40, 245, 0.12)",
            border: "1px solid rgba(130, 40, 245, 0.25)",
            borderRadius: "var(--radius-lg)",
          }}
        >
          <Shield size={28} style={{ color: "var(--accent)" }} />
        </div>
        <h1 className="text-3xl font-bold tracking-tight">
          Welcome to Code Atelier Governance
        </h1>
        <p
          className="text-base max-w-md mx-auto leading-relaxed"
          style={{ color: "var(--text-secondary)" }}
        >
          Your dashboard is ready. Connect your first agent to start
          monitoring scope, spend, and approvals in real time.
        </p>
      </div>

      {/* Feature icon grid */}
      <div className="grid grid-cols-3 gap-3">
        {FEATURE_ICONS.map(({ icon: Icon, label }) => (
          <div
            key={label}
            className="flex flex-col items-center gap-2 py-4 border"
            style={{
              background: "var(--card)",
              borderColor: "var(--border)",
              borderRadius: "var(--radius-md)",
            }}
          >
            <Icon
              size={20}
              style={{ color: "var(--text-tertiary)" }}
            />
            <span
              className="text-xs"
              style={{ color: "var(--text-tertiary)" }}
            >
              {label}
            </span>
          </div>
        ))}
      </div>

      {/* Steps */}
      <div>
        <h2
          className="text-sm font-semibold uppercase tracking-wider mb-5"
          style={{ color: "var(--text-tertiary)" }}
        >
          Get started in 5 steps
        </h2>
        <div className="space-y-1">
          <Step
            number={1}
            title="Install the SDK"
            code="pip install codeatelier-governance"
          />
          <Step
            number={2}
            title="Set your audit secret"
            code={`export GOVERNANCE_AUDIT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')`}
          />
          <Step
            number={3}
            title="Apply the schema to your Postgres"
            code={`# Using the governance CLI:\ngovernance migrate --database-url postgresql://...\n\n# Or manually with psql:\npsql $DATABASE_URL -f $(python -c 'import codeatelier_governance.audit, os; print(os.path.join(os.path.dirname(codeatelier_governance.audit.__file__), "ddl.sql"))')`}
          />
          <Step
            number={4}
            title="Log your first audit event"
            code={`from codeatelier_governance import GovernanceSDK
from codeatelier_governance.audit import AuditEvent

sdk = GovernanceSDK(database_url="postgresql://...")
await sdk.start()

record = await sdk.audit.log(
    AuditEvent(agent_id="my-first-agent", kind="hello.world")
)
print(f"First event logged: {record.event_id}")`}
          />
          <Step
            number={5}
            title="Refresh this page"
            code=""
            description="Your agent should appear on the Governance Posture dashboard with one event and all-green status badges."
          />
        </div>
      </div>

      <div
        className="text-center text-sm pt-2"
        style={{ color: "var(--text-tertiary)" }}
      >
        <p>
          Need help?{" "}
          <a
            href="https://codeatelier.tech/governance"
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: "var(--accent)" }}
            className="hover:underline"
          >
            Read the docs
          </a>
        </p>
      </div>
    </div>
  );
}

function Step({
  number,
  title,
  code,
  description,
}: {
  number: number;
  title: string;
  code: string;
  description?: string;
}) {
  return (
    <details className="group">
      <summary
        className="flex items-center gap-3 cursor-pointer py-3 px-3 -mx-3 select-none transition-colors"
        style={{ borderRadius: "var(--radius-md)" }}
      >
        <div
          className="flex-shrink-0 w-7 h-7 flex items-center justify-center text-xs font-bold"
          style={{
            background: "rgba(130, 40, 245, 0.15)",
            color: "var(--accent)",
            borderRadius: "var(--radius-sm)",
          }}
        >
          {number}
        </div>
        <span className="font-medium text-sm flex-1">{title}</span>
        <CheckCircle2
          size={16}
          className="opacity-0 group-open:opacity-100 transition-opacity"
          style={{ color: "var(--success)" }}
        />
      </summary>
      <div className="pl-10 pb-3">
        {description && (
          <p
            className="text-sm mb-2"
            style={{ color: "var(--text-tertiary)" }}
          >
            {description}
          </p>
        )}
        {code && (
          <pre
            className="text-sm font-mono overflow-x-auto whitespace-pre-wrap p-3"
            style={{
              background: "rgba(0, 0, 0, 0.4)",
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border)",
            }}
          >
            {code}
          </pre>
        )}
      </div>
    </details>
  );
}
