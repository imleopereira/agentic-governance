"use client";

export function GettingStarted() {
  return (
    <div className="max-w-2xl mx-auto py-16 text-center space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight mb-2">
          Welcome to Code Atelier Governance
        </h1>
        <p style={{ color: "var(--text-tertiary)" }}>
          Your dashboard is empty because no agents have logged events yet.
          Follow these steps to get your first data flowing.
        </p>
      </div>

      <div className="text-left space-y-6">
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

      <div className="pt-4 text-sm" style={{ color: "var(--text-tertiary)" }}>
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
    <div className="flex gap-4">
      <div
        className="flex-shrink-0 w-8 h-8 flex items-center justify-center text-sm font-bold"
        style={{
          background: "rgba(130, 40, 245, 0.15)",
          color: "var(--accent)",
          borderRadius: "var(--radius-sm)",
        }}
      >
        {number}
      </div>
      <div className="flex-1">
        <h3 className="font-semibold mb-1">{title}</h3>
        {description && (
          <p className="text-sm mb-2" style={{ color: "var(--text-tertiary)" }}>
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
    </div>
  );
}
