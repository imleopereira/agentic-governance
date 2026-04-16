/**
 * Runtime metadata-field validators for SSE/hydrated audit events.
 *
 * Today `useEventStream.ts` and `EnforcementTrace.tsx` perform several
 * unsafe `String(event.metadata?.foo)` coercions. Those paths cannot be
 * proven safe at compile time because `metadata` is typed
 * `Record<string, unknown>` over the wire — a drifted backend or a
 * hostile SDK caller can land any scalar there, including numbers,
 * bigints, arrays, or `undefined`.
 *
 * This module centralises the narrowing rules so each reader stops
 * rolling its own `as string` and instead receives a typed optional.
 *
 * Wiring note: NOT wired into `useEventStream.ts` in v0.6. F2 touched
 * the hook in this release and F1 is editing adjacent surfaces; a
 * runtime swap here would collide. The helper ships as an isolated
 * module and will be wired in v0.6.1.
 *
 * The narrowing rules are intentionally conservative: only JSON scalars
 * already allowed by the Pydantic `MetadataValue` alias in
 * `src/codeatelier_governance/console/models/responses.py` pass through.
 * Anything else returns `null` and the caller decides whether to drop
 * the field or log a drift warning.
 */

const MAX_LEN = 1024;

export interface ValidatedMetadata {
  model: string | null;
  tool: string | null;
  request_id: string | null;
}

function coerceString(v: unknown): string | null {
  if (typeof v !== "string") return null;
  if (v.length === 0 || v.length > MAX_LEN) return null;
  return v;
}

/**
 * Narrow the three metadata fields every console reader currently
 * coerces by hand: `model`, `tool`, `request_id`.
 *
 * Returns a typed object with each field either a bounded string or
 * `null`. Never throws.
 */
export function validateMetadata(
  raw: Record<string, unknown> | null | undefined,
): ValidatedMetadata {
  if (!raw || typeof raw !== "object") {
    return { model: null, tool: null, request_id: null };
  }
  return {
    model: coerceString(raw.model),
    tool: coerceString(raw.tool),
    request_id: coerceString(raw.request_id),
  };
}

/** Single-field helpers for the EnforcementTrace inline-card display. */
export const validateMetadataField = {
  model: (raw: unknown): string | null => coerceString(raw),
  tool: (raw: unknown): string | null => coerceString(raw),
  requestId: (raw: unknown): string | null => coerceString(raw),
};
