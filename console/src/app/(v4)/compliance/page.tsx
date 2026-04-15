"use client";

/**
 * F4 full compliance report page.
 *
 * Consumes ``GET /api/compliance/report`` and renders the Article 12
 * evidence summary: chain integrity, the verification window, coverage
 * percentage (with caveat), total events audited, total agents.
 *
 * Empty state: when no audit backend is configured the endpoint
 * returns a 503; the page shows "No compliance report yet" rather
 * than a crash.
 */

import { useEffect, useState } from "react";
import { translateCoverageCaveat } from "@/lib/caveats";
import { api, type ComplianceReportView } from "@/lib/api";

export default function ComplianceReportPage() {
  const [report, setReport] = useState<ComplianceReportView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.getComplianceReport();
        if (!cancelled) {
          setReport(res);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Unknown error");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return (
      <main
        className="p-6"
        aria-busy="true"
        aria-label="Loading compliance report"
      >
        <h1 className="text-xl font-semibold">Compliance report</h1>
        <p className="text-sm opacity-70">Loading...</p>
      </main>
    );
  }

  if (error || !report) {
    return (
      <main className="p-6" aria-label="Compliance report">
        <h1 className="text-xl font-semibold">Compliance report</h1>
        <p className="mt-4 text-sm opacity-70">
          No compliance report yet. Once audit events are captured the
          Article&nbsp;12 summary will appear here.
        </p>
        {error ? (
          <p className="mt-2 text-xs text-[color:var(--danger)]">
            {error}
          </p>
        ) : null}
      </main>
    );
  }

  const caveat = translateCoverageCaveat(report.coverage_pct_reason);
  const windowLabel =
    report.chain_verified_from_seq !== null &&
    report.chain_verified_to_seq !== null
      ? `Events ${report.chain_verified_from_seq}–${report.chain_verified_to_seq} verified`
      : "Verification window unavailable";

  const verifiedAt = report.generated_at
    ? new Date(report.generated_at).toUTCString()
    : "unknown";

  return (
    <main className="p-6 space-y-6" aria-label="Compliance report">
      <header>
        <h1 className="text-xl font-semibold">Compliance report</h1>
        <p className="text-xs opacity-70">
          Article&nbsp;12 evidence summary · generated {verifiedAt}
        </p>
      </header>

      <section aria-labelledby="chain-heading" className="space-y-1">
        <h2 id="chain-heading" className="text-sm font-medium">
          Chain integrity
        </h2>
        <p className="text-sm">
          Status:{" "}
          <span data-testid="chain-status">
            {report.chain_integrity_status}
          </span>
        </p>
        <p className="text-xs opacity-70">{windowLabel}</p>
      </section>

      <section aria-labelledby="coverage-heading" className="space-y-1">
        <h2 id="coverage-heading" className="text-sm font-medium">
          Wrapper coverage
        </h2>
        <p className="text-sm">
          {report.coverage_pct === null
            ? "Not measured"
            : `${(report.coverage_pct * 100).toFixed(1)}%`}
        </p>
        {caveat ? (
          <p className="text-xs opacity-70" role="note">
            {caveat}
          </p>
        ) : null}
      </section>

      <section aria-labelledby="scope-heading" className="space-y-1">
        <h2 id="scope-heading" className="text-sm font-medium">
          Scope
        </h2>
        <ul className="text-sm list-disc pl-5">
          <li>
            Events audited: <strong>{report.total_events_audited}</strong>
          </li>
          <li>
            Agents observed: <strong>{report.total_agents}</strong>
          </li>
        </ul>
        {report.coverage_caveat ? (
          <p className="text-xs opacity-70" role="note">
            {report.coverage_caveat}
          </p>
        ) : null}
      </section>
    </main>
  );
}
