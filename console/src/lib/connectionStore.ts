/**
 * Lightweight Zustand store for tracking governance backend connectivity.
 *
 * Driven by the global QueryCache onError handler (see queryClient.ts).
 * UI shell subscribes to this to render the v4 DisconnectBanner while
 * still showing cached data from React Query — satisfies invariant #1
 * (host app / console must keep working when governance DB is unreachable).
 */

import { create } from "zustand";

/**
 * Security H3: strip common secret-bearing patterns from raw error
 * messages before they land in the store (and subsequently in rendered
 * UI / error boundaries / screenshots). Truncates to 120 chars.
 *
 * Heuristic only — not a replacement for the server never including
 * secrets in error bodies — but prevents the common accident of a
 * fetch error echoing `Authorization: Bearer sk-...` into the DOM.
 *
 * Two-pass strategy:
 *   1. Strip RFC 7235 auth scheme tokens first: `Bearer <secret>`,
 *      `Basic <base64>`, `Digest ...`. This catches the case where a
 *      secret trails a header keyword — e.g. `Authorization: Bearer
 *      sk-abc123` — which the keyword-value pass alone misses because
 *      it only consumes the single `\S+` after the keyword (the word
 *      `Bearer`), leaving the actual secret dangling.
 *   2. Strip `keyword[=:\s]+value` patterns for common secret-bearing
 *      parameter names (token=, api_key=, password:, etc.).
 *
 * Truncation runs after both passes so a secret near the 120-char
 * boundary can never be partially exposed by the cut.
 */
export function sanitizeErrorMessage(raw: string): string {
  // Pass 1: auth scheme tokens.
  let s = raw.replace(
    /\b(bearer|basic|digest)\s+\S+/gi,
    "$1 [redacted]",
  );
  // Pass 2: keyword-value patterns.
  s = s.replace(
    /(bearer|token|key|secret|password|authorization)[=:\s]+\S+/gi,
    "$1=[redacted]",
  );
  return s.length > 120 ? s.slice(0, 117) + "..." : s;
}

export type ConnectionHealth = "ok" | "degraded" | "offline";

export interface ConnectionStoreState {
  status: ConnectionHealth;
  lastError: string | null;
  consecutiveErrors: number;
  setStatus: (status: ConnectionHealth, lastError?: string | null) => void;
  reportError: (error: Error) => void;
  reportSuccess: () => void;
}

/** Threshold of consecutive query errors before we flip to "offline". */
const OFFLINE_THRESHOLD = 3;

export const useConnectionStore = create<ConnectionStoreState>((set, get) => ({
  status: "ok",
  lastError: null,
  consecutiveErrors: 0,
  setStatus: (status, lastError = null) =>
    set({
      status,
      lastError: lastError === null ? null : sanitizeErrorMessage(lastError),
      consecutiveErrors: status === "ok" ? 0 : get().consecutiveErrors,
    }),
  reportError: (error) => {
    const next = get().consecutiveErrors + 1;
    set({
      consecutiveErrors: next,
      lastError: sanitizeErrorMessage(error.message),
      status: next >= OFFLINE_THRESHOLD ? "offline" : "degraded",
    });
  },
  reportSuccess: () =>
    set({ status: "ok", consecutiveErrors: 0, lastError: null }),
}));
