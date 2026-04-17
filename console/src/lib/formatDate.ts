/**
 * Shared UTC date formatters for the v4 console.
 *
 * Compliance-facing surfaces (Article 12 report, evidence bundle
 * filenames) must present timestamps in UTC with a single consistent
 * format. Mixing `toUTCString()` (RFC 7231, "Fri, 17 Apr 2026 …") with
 * ISO-8601 in the same view reads as "vendor doesn't know which clock
 * this is on" to a CTO / compliance officer.
 *
 * These helpers intentionally avoid `Intl.DateTimeFormat` locale
 * surprises: we want the same string on every reviewer's machine.
 */

/**
 * Format an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM:SS UTC``.
 *
 * Returns ``"unknown"`` for falsy / unparseable input so callers can
 * render the label without a conditional.
 */
export function formatUtcTimestamp(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  const y = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
  const da = String(d.getUTCDate()).padStart(2, "0");
  const h = String(d.getUTCHours()).padStart(2, "0");
  const mi = String(d.getUTCMinutes()).padStart(2, "0");
  const s = String(d.getUTCSeconds()).padStart(2, "0");
  return `${y}-${mo}-${da} ${h}:${mi}:${s} UTC`;
}

/**
 * Format an ISO-8601 timestamp as the filename-safe date segment
 * ``YYYY-MM-DD``. Used to compose human-readable evidence bundle
 * filenames (e.g. ``compliance-evidence-2026-03-17_to_2026-04-16.json``)
 * that an auditor can sort, grep, and recognise at a glance.
 *
 * Falls back to ``"unknown"`` for unparseable input. The caller is
 * responsible for deciding whether to surface that string or refuse
 * the download.
 */
export function formatUtcDateForFilename(
  iso: string | null | undefined,
): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  const y = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, "0");
  const da = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${mo}-${da}`;
}
