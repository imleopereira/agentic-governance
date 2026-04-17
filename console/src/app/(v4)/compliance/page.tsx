"use client";

/**
 * F4 full compliance report page (v4 redesign).
 *
 * Consumes ``GET /api/compliance/report`` and renders the Article 12
 * evidence summary in a hero-first layout. Designer A owns information
 * architecture + copy (hero-first ordering, progressive disclosure,
 * human-readable filename, formatDate tokens). Designer B owns visual
 * density on top:
 *
 *   - Hero row: heroic event count (mono, 36px) left, Export button
 *     (primary violet) right.
 *   - Status row: three inline Pills reusing the `/agents` header
 *     visual language so `verified` reads the same everywhere.
 *   - Details: three cards in a CSS grid with the shared `.card`
 *     utility so containers match the rest of the console.
 *   - Download toast: fade-in 150ms, linger 6s, fade-out 300ms,
 *     opacity-only (no translate) so it doesn't rip off-screen.
 *
 * Empty state: when no audit backend is configured the endpoint
 * returns a 503; the page shows "No compliance report yet" rather
 * than a crash.
 */

import { useEffect, useState } from "react";
import { CheckCircle2, Download } from "lucide-react";
import { translateCoverageCaveat } from "@/lib/caveats";
import { api, type ComplianceReportView } from "@/lib/api";
import {
  formatUtcTimestamp,
  formatUtcDateForFilename,
} from "@/lib/formatDate";
import { Pill, type PillTone } from "@/components/v4/Pill";

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
  // Human-readable download confirmation shown inline under the Export
  // button after a successful download. The filename is the same one
  // the browser saved, so an auditor can visually match what's on disk.
  const [lastDownloadedFilename, setLastDownloadedFilename] = useState<
    string | null
  >(null);
  // Visual success toast (sighted users). Drives a fade-in/linger/
  // fade-out lifecycle via CSS opacity transitions. Separate from
  // `bundleStatus` (SR-only live region) and from `lastDownloadedFilename`
  // (inline persistent confirmation under the Export button owned by
  // Designer A) so each surface can live its own lifespan.
  const [toastVisible, setToastVisible] = useState(false);

  async function handleDownloadBundle(): Promise<void> {
    if (bundleLoading) return;
    setBundleLoading(true);
    setBundleError(null);
    setBundleStatus("Preparing evidence bundle.");
    try {
      const bundle = await api.exportComplianceBundle();
      // Human-readable filename: ``compliance-evidence-YYYY-MM-DD_to_YYYY-MM-DD.json``.
      // Replaces the v3 double-ISO-with-microseconds format which was
      // unreadable on camera and collided with Windows filesystem
      // restrictions (`:` / `.`). UTC dates only — no times — since
      // bundle windows are always day-aligned in the backend and the
      // internal ``window`` object already records the exact
      // microsecond boundaries for verification.
      const start = formatUtcDateForFilename(bundle.window.start);
      const end = formatUtcDateForFilename(bundle.window.end);
      const filename = `compliance-evidence-${start}_to_${end}.json`;
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
      setLastDownloadedFilename(filename);
      setBundleStatus(`Evidence bundle downloaded as ${filename}.`);
      // Toast: fade-in (CSS 150ms) + linger (6s) + fade-out (CSS
      // 300ms). The visible->hidden flip happens on the 6s timer;
      // opacity transition-duration on the element handles each tail.
      setToastVisible(true);
      window.setTimeout(() => setToastVisible(false), 6_000);
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
  const verifiedAt = formatUtcTimestamp(report.generated_at);
  const chainStatus = report.chain_integrity_status;
  const chainVerified = chainStatus === "verified";
  const eventsWindow =
    report.chain_verified_from_seq !== null &&
    report.chain_verified_to_seq !== null
      ? `Events ${report.chain_verified_from_seq}\u2013${report.chain_verified_to_seq}`
      : "Verification window unavailable";

  // Pill tone for the chain-integrity status. Mirrors the palette used
  // by `ComplianceHeaderPill` so the two surfaces are visually identical.
  const chainTone: PillTone = chainVerified
    ? "success"
    : chainStatus === "halted" || chainStatus === "degraded"
      ? "danger"
      : "warn";

  // Wrapper coverage pill. `not measured` is the OUT-OF-THE-BOX default
  // (wrapper coverage is opt-in via `enable_coverage=True`). Render it
  // in the muted `skip` tone so the CTO's eye does NOT read it as a
  // failure — it's an informational "feature is off".
  const coverageMeasured = report.coverage_pct !== null;
  const coverageLabel = coverageMeasured
    ? `${((report.coverage_pct as number) * 100).toFixed(1)}%`
    : "not measured";
  const coverageTone: PillTone =
    coverageMeasured && (report.coverage_pct as number) >= 1
      ? "success"
      : "skip";

  return (
    <main className="p-6 space-y-8" aria-label="Compliance report">
      {/* --- HERO -----------------------------------------------------
          Designer A: hero-first IA (summary above, drill below).
          Designer B: two-column split — heroic number left, Export
          button right — so a 1:10 screen-recording pause lands the
          CTO on ONE fact ("248 events · chain intact") and ONE
          action ("Export Article 12 evidence"). */}
      <header className="space-y-5">
        <div className="space-y-1">
          <p className="text-xs uppercase tracking-wider text-[color:var(--text-tertiary)]">
            Article&nbsp;12 evidence
          </p>
          <h1 className="text-xl font-semibold" data-testid="chain-headline">
            Compliance report
          </h1>
        </div>

        <div className="flex flex-col gap-5 sm:flex-row sm:items-start sm:justify-between">
          {/* Left column: the ONE number a CTO remembers. Mono +
              tabular-nums so digits don't jitter on re-render. */}
          <div className="space-y-2">
            <div className="flex items-baseline gap-3 font-mono tabular-nums">
              <span
                className="text-4xl font-semibold leading-none tracking-tight text-[color:var(--fg)]"
                data-testid="events-audited"
              >
                {report.total_events_audited.toLocaleString()}
              </span>
              <span className="text-sm text-[color:var(--text-secondary)]">
                events · chain{" "}
                {chainVerified ? "intact" : chainStatus}
              </span>
            </div>
            <p className="text-xs text-[color:var(--text-tertiary)]">
              {eventsWindow} · last verified {verifiedAt}
            </p>
          </div>

          {/* Right column: primary export action. Uses the shared
              `.btn-primary` utility (violet brand accent) so it reads
              as "the thing to click" without fighting the hero metric
              for weight. */}
          <div className="flex flex-col items-start gap-2 sm:items-end">
            <button
              type="button"
              onClick={handleDownloadBundle}
              disabled={bundleLoading}
              aria-busy={bundleLoading}
              aria-describedby="evidence-bundle-help evidence-bundle-error evidence-bundle-status evidence-bundle-confirmation"
              data-testid="download-evidence-bundle"
              className="btn-primary"
            >
              <Download size={14} aria-hidden="true" />
              {bundleLoading
                ? "Preparing evidence\u2026"
                : "Export Article 12 evidence"}
            </button>
            {/* Inline confirmation shown after a successful download
                (Designer A owned this surface). Kept under the button
                so sighted users can verify the file on disk matches. */}
            {lastDownloadedFilename ? (
              <p
                id="evidence-bundle-confirmation"
                className="text-right text-xs text-[color:var(--text-secondary)]"
              >
                Downloaded ·{" "}
                <span
                  className="font-mono"
                  data-testid="downloaded-filename"
                >
                  {lastDownloadedFilename}
                </span>
              </p>
            ) : (
              <span id="evidence-bundle-confirmation" className="sr-only" />
            )}
            {/* Polite live region for export lifecycle (non-error).
                Always rendered so SR picks up status changes; empty
                when idle. Visually hidden — the button label already
                tells sighted users we're "Preparing evidence…". */}
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
        </div>

        {/* --- STATUS PILL ROW -----------------------------------------
            Three inline pills in the SAME visual language as the
            top-right `ComplianceHeaderPill` (shared Pill component,
            same tone palette). Camera-friendly: a CTO who has seen
            `verified` in the header already knows what this means. */}
        <div
          className="flex flex-wrap items-center gap-2"
          data-testid="status-pill-row"
        >
          <Pill tone={chainTone} title="Chain integrity">
            <span className="mr-1 opacity-70">chain</span>
            <span data-testid="chain-status">{chainStatus}</span>
          </Pill>
          <Pill tone={coverageTone} title="Wrapper coverage">
            <span className="mr-1 opacity-70">coverage</span>
            {coverageLabel}
          </Pill>
          <Pill tone="neutral" title="Last verified (UTC)">
            <span className="mr-1 opacity-70">verified</span>
            {verifiedAt}
          </Pill>
        </div>
      </header>

      {/* --- DRILL-DOWN -----------------------------------------------
          Visually separated from the hero with a rule so the camera
          eye reads "summary above, details below". Everything here is
          supporting evidence; none of it is the headline. Three cards
          in a CSS grid with the shared `.card` utility so containers
          match the rest of the console (same border, radius, shadow). */}
      <div
        aria-hidden="true"
        style={{ borderTop: "1px solid var(--border)" }}
      />

      <section
        aria-label="Details"
        className="grid grid-cols-1 gap-4 md:grid-cols-3"
      >
        <article
          aria-labelledby="chain-detail-heading"
          className="card p-4 space-y-2"
        >
          <h2
            id="chain-detail-heading"
            className="text-xs font-medium uppercase tracking-wider text-[color:var(--text-tertiary)]"
          >
            Chain integrity
          </h2>
          <p className="text-sm text-[color:var(--fg)]">
            Status:{" "}
            <span
              className="font-medium"
              data-testid="chain-status-detail"
              style={{
                color:
                  chainTone === "success"
                    ? "var(--success)"
                    : chainTone === "danger"
                      ? "var(--danger)"
                      : "var(--warn)",
              }}
            >
              {chainStatus}
            </span>
          </p>
          <p className="text-xs text-[color:var(--text-tertiary)]">
            {eventsWindow}
          </p>
        </article>

        <article
          aria-labelledby="coverage-heading"
          className="card p-4 space-y-2"
          data-testid="coverage-section"
        >
          <h2
            id="coverage-heading"
            className="text-xs font-medium uppercase tracking-wider text-[color:var(--text-tertiary)]"
          >
            Wrapper coverage
          </h2>
          <p className="text-sm text-[color:var(--fg)]">
            {report.coverage_pct === null
              ? "Not measured"
              : `${(report.coverage_pct * 100).toFixed(1)}%`}
          </p>
          {caveat ? (
            <p
              className="text-xs text-[color:var(--text-tertiary)]"
              role="note"
            >
              {caveat}
            </p>
          ) : null}
        </article>

        <article
          aria-labelledby="scope-heading"
          className="card p-4 space-y-2"
        >
          <h2
            id="scope-heading"
            className="text-xs font-medium uppercase tracking-wider text-[color:var(--text-tertiary)]"
          >
            Scope
          </h2>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
            <dt className="text-[color:var(--text-tertiary)]">Events</dt>
            <dd className="text-right font-mono tabular-nums text-[color:var(--fg)]">
              {report.total_events_audited.toLocaleString()}
            </dd>
            <dt className="text-[color:var(--text-tertiary)]">Agents</dt>
            <dd className="text-right font-mono tabular-nums text-[color:var(--fg)]">
              {report.total_agents.toLocaleString()}
            </dd>
          </dl>
          {report.coverage_caveat ? (
            <p
              className="text-xs text-[color:var(--text-tertiary)]"
              role="note"
            >
              {report.coverage_caveat}
            </p>
          ) : (
            <p className="text-xs text-[color:var(--text-tertiary)]">
              This report covers only actions routed through the SDK.
              Calls made outside the SDK wrapper are not logged and are
              not reflected in this report.
            </p>
          )}
        </article>
      </section>

      {/* Explanatory help moved below the cards — no longer competing
          with the Export button in the hero. */}
      <p
        id="evidence-bundle-help"
        className="max-w-2xl text-xs text-[color:var(--text-tertiary)]"
      >
        Signed JSON bundle. Chain verification + coverage report,
        HMAC-signed for regulator handoff.
      </p>

      {/* --- DOWNLOAD TOAST ------------------------------------------
          Fade-in 150ms, linger 6s, fade-out 300ms. Opacity-only (no
          translate) so it settles without ripping off-screen. Uses
          --success tokens via inline style so the color stays tied
          to the design-token palette. Purely decorative — the same
          message is in the SR live region and the inline confirmation
          under the Export button, so aria-hidden is correct. */}
      {lastDownloadedFilename ? (
        <div
          aria-hidden="true"
          data-testid="download-toast"
          className={`fixed bottom-6 right-6 z-40 flex items-center gap-2 rounded-md border px-3 py-2 text-xs shadow-[var(--shadow-card)] transition-opacity ${
            toastVisible
              ? "opacity-100 duration-150"
              : "pointer-events-none opacity-0 duration-300"
          }`}
          style={{
            background: "rgba(34, 197, 94, 0.08)",
            borderColor: "rgba(34, 197, 94, 0.4)",
            color: "var(--success)",
          }}
        >
          <CheckCircle2 size={14} aria-hidden="true" />
          <span className="font-mono tabular-nums">
            Downloaded {lastDownloadedFilename}
          </span>
        </div>
      ) : null}
    </main>
  );
}
