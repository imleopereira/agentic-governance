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
  // Bundle-export UI state. Intentionally separate from the report
  // loader's state: the caveat display must not flicker while a bundle
  // download is in flight, and an export failure must not hide the
  // already-rendered report.
  const [bundleLoading, setBundleLoading] = useState(false);
  const [bundleError, setBundleError] = useState<string | null>(null);
  // Live status for screen-reader announcement of the export lifecycle.
  // Separate from `bundleError` (which uses role="alert"): success /
  // in-progress use polite role="status" so we don't interrupt the SR
  // buffer for non-urgent updates.
  const [bundleStatus, setBundleStatus] = useState<string | null>(null);

  // ISO-8601 timestamps contain `:` and `.`, both invalid on Windows
  // filesystems (NTFS + explorer.exe reject them). Strip to produce a
  // cross-platform-safe filename segment while keeping the stamp legible
  // to an auditor. DA tagged the `:` collision; fixing `.` at the same
  // time so sub-second components don't break either.
  function safeFilenameStamp(iso: string): string {
    return iso.replace(/[:.]/g, "-");
  }

  async function handleDownloadBundle(): Promise<void> {
    if (bundleLoading) return;
    setBundleLoading(true);
    setBundleError(null);
    setBundleStatus("Preparing evidence bundle.");
    try {
      const bundle = await api.exportComplianceBundle();
      const filename = `compliance-evidence-${safeFilenameStamp(
        bundle.window.start,
      )}-${safeFilenameStamp(bundle.window.end)}.json`;
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {
        type: "application/json",
      });
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(href);
      setBundleStatus(`Evidence bundle downloaded as ${filename}.`);
    } catch (e) {
      // TODO(v0.6.2-ui): parse 429 response + Retry-After header and show
      // "try again in N s" inline; currently a rate-limited retry
      // surfaces a generic error message. Product-level UX decision
      // (e.g. countdown vs toast vs disabled button with timer), hence
      // a TODO rather than a DA-time fix.
      setBundleError(e instanceof Error ? e.message : "Download failed.");
      setBundleStatus(null);
    } finally {
      setBundleLoading(false);
    }
  }

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
      <header className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold">Compliance report</h1>
          <p className="text-xs opacity-70">
            Article&nbsp;12 evidence summary · generated {verifiedAt}
          </p>
        </div>
        <div className="flex flex-col items-start gap-1 sm:items-end">
          <button
            type="button"
            onClick={handleDownloadBundle}
            disabled={bundleLoading}
            aria-busy={bundleLoading}
            aria-describedby="evidence-bundle-help evidence-bundle-error evidence-bundle-status"
            data-testid="download-evidence-bundle"
            className="inline-flex items-center gap-2 rounded-md border border-[color:var(--border,#d4d4d4)] bg-[color:var(--surface,transparent)] px-3 py-1.5 text-sm font-medium shadow-sm transition hover:bg-[color:var(--surface-hover,rgba(0,0,0,0.04))] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {bundleLoading ? "Preparing evidence…" : "Export Article 12 evidence"}
          </button>
          <p
            id="evidence-bundle-help"
            className="max-w-xs text-right text-xs opacity-70"
          >
            Signed JSON bundle. Chain verification + coverage report, HMAC-signed
            for regulator handoff.
          </p>
          {/* Polite live region for export lifecycle (non-error).
              Always rendered so SR picks up status changes; empty when
              idle. Visually hidden — the button label already tells
              sighted users we're "Preparing evidence…". */}
          <p
            id="evidence-bundle-status"
            role="status"
            aria-live="polite"
            className="sr-only"
          >
            {bundleStatus ?? ""}
          </p>
          {bundleError ? (
            <p
              id="evidence-bundle-error"
              role="alert"
              className="max-w-xs text-right text-xs text-[color:var(--danger)]"
            >
              {bundleError}
            </p>
          ) : (
            // Empty placeholder with the same id so aria-describedby
            // stays valid when no error is present.
            <span id="evidence-bundle-error" className="sr-only" />
          )}
        </div>
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
