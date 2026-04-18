/**
 * F4 coverage caveat translator.
 *
 * The backend ``ComplianceReport`` carries a discriminator string in
 * ``coverage_pct_reason`` that tells consumers why ``coverage_pct`` is
 * what it is. This module maps the developer-facing discriminator to
 * the user-facing copy the compliance page renders.
 *
 * Unknown discriminators do NOT throw — we return a generic disclosure
 * so a new backend value ships a safe default rather than a crash.
 */

export type CoveragePctReason =
  | "no_scope_policies_registered"
  | "registry_disabled"
  | "ok"
  | string;

const KNOWN: Record<string, string | null> = {
  ok: null,
  // v0.6: compliance-officer voice. The v3 copy referenced
  // ``enable_coverage=True`` — a Python flag name leaking into the
  // v4 Article 12 surface. The SDK-config phrasing keeps the caveat
  // actionable without turning the compliance page into developer docs.
  registry_disabled:
    "Not measured. Enable wrapper coverage in SDK config to track agent coverage.",
  no_scope_policies_registered:
    "No scope policies exist in this deployment, so coverage is undefined.",
};

/**
 * Translate a ``coverage_pct_reason`` value to the user-facing copy
 * that should appear beside ``coverage_pct`` in the compliance page.
 *
 * Returns:
 *   - ``null`` when the reason is ``"ok"`` (nothing to disclose).
 *   - the mapped copy for a known reason.
 *   - a generic fallback for an unknown reason.
 */
export function translateCoverageCaveat(
  reason: string | null | undefined,
): string | null {
  if (!reason) return null;
  if (reason in KNOWN) {
    return KNOWN[reason];
  }
  return `Coverage note: ${reason}`;
}
