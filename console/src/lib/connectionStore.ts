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
 * Multi-pass strategy (order matters):
 *   1. Strip database DSNs (`postgres://`, `postgresql://`, `mysql://`,
 *      `mongodb://`, `redis://`) BEFORE any URL-ish pattern, with or
 *      without embedded credentials. DSNs frequently leak in connection
 *      error messages (`could not connect to postgres://user:pw@h/db`)
 *      and any embedded user:pass is the highest-value secret in the
 *      whole message.
 *   2. Strip filesystem paths (Unix absolute paths under well-known
 *      filesystem roots like `/Users`, `/home`, `/etc`, `/var`, `/tmp`,
 *      etc.; Windows drive-letter paths like `C:\foo\bar`). We
 *      deliberately do NOT strip generic `/api/...` URL paths because
 *      those are useful debugging context and not a secret. Paths run
 *      BEFORE IP stripping so an IP embedded in a path still gets
 *      redacted via the path rule (and the path matcher is anchored on
 *      filesystem roots, so it won't gobble digits-as-IPs by accident).
 *   3. Strip IPv6 addresses (longest-first to win over IPv4 chunks).
 *   4. Strip IPv4 addresses (dotted quad with each octet 0-255).
 *   5. Strip RFC 7235 auth scheme tokens: `Bearer <secret>`,
 *      `Basic <base64>`, `Digest ...`. Catches the case where a secret
 *      trails a header keyword — `Authorization: Bearer sk-abc123` —
 *      which the keyword-value pass alone misses because it only
 *      consumes the single `\S+` after the keyword (the word
 *      `Bearer`), leaving the actual secret dangling.
 *   6. Strip `keyword[=:\s]+value` patterns for common secret-bearing
 *      parameter names (token=, api_key=, password:, etc.).
 *   7. Strip bare long-hex runs (32+ hex chars). Covers 32-byte HMAC
 *      keys / AUDIT_SECRET / SHA256 digests / Ed25519 fingerprints that
 *      a server could echo into an error body without a surrounding
 *      `token=` keyword. Runs AFTER the named passes so a DSN or Bearer
 *      header still gets its contextual `[REDACTED]` form instead of
 *      collapsing to the catch-all.
 *   8. Strip bare long-base64 runs (40+ chars, optional `=` padding).
 *      Covers Ed25519 signatures (88 chars base64), base64-encoded raw
 *      keys, JWT payload/signature chunks. Word-boundary anchored on
 *      both sides so short tokens, URL hostnames (dot-terminated), and
 *      hyphen-bearing identifiers (Tailwind classes like `bg-neutral-900`)
 *      are preserved.
 *
 * Truncation runs after every pass so a secret near the 120-char
 * boundary can never be partially exposed by the cut.
 */
export function sanitizeErrorMessage(raw: string): string {
  // Pass 1: database DSNs (must run BEFORE any URL/IP/path heuristic so
  // embedded user:password@host pairs can never leak).
  let s = raw.replace(
    /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss):\/\/\S+/gi,
    "[REDACTED]",
  );
  // Pass 2: filesystem paths. Anchored on known FS roots so we don't
  // eat HTTP/REST URL paths like `/api/agents`.
  s = s.replace(
    /(?:\/(?:Users|home|root|etc|var|tmp|opt|usr|private|mnt|srv|proc|sys|dev)(?:\/[^\s'"`]*)?)/g,
    "[REDACTED]",
  );
  // Windows drive-letter paths: C:\foo\bar  or  C:/foo/bar
  s = s.replace(/\b[A-Za-z]:[\\/](?:[^\s'"`<>|]+)/g, "[REDACTED]");
  // Pass 3: IPv6 (run before IPv4 — IPv6 may end in an embedded IPv4
  // chunk like ::ffff:192.0.2.1 that we want consumed as a single unit).
  // Loose but bounded: 2+ hex groups joined by `:`, optional `::`
  // collapse, optional trailing dotted-quad.
  //
  // v0.6.1 DA fix: the match MUST contain EITHER a hex letter (a-f)
  // OR a `::` shorthand. Without this the pattern matches pure-digit
  // line:col references like `ErrorBoundary.tsx:93:15`, which are
  // common in React stack frames and debug output. Real IPv6
  // addresses either carry hex letters (the vast majority) or use
  // the `::` collapse (e.g. ``::1``); a bare ``a.b.c.d.e`` run of
  // short digit groups is almost certainly not an IP.
  s = s.replace(
    /(?<![0-9a-f:])(?=[0-9a-f:.]*(?:[a-f]|::))(?:[0-9a-f]{1,4}:|:){2,}[0-9a-f]{1,4}(?:(?::|\.)[0-9a-f.]+)*/gi,
    "[REDACTED]",
  );
  // Pass 4: IPv4 dotted quad with each octet 0-255.
  s = s.replace(
    /\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b/g,
    "[REDACTED]",
  );
  // Pass 5: auth scheme tokens.
  s = s.replace(
    /\b(bearer|basic|digest)\s+\S+/gi,
    "$1 [redacted]",
  );
  // Pass 6: keyword-value patterns.
  s = s.replace(
    /(bearer|token|key|secret|password|authorization)[=:\s]+\S+/gi,
    "$1=[redacted]",
  );
  // Pass 7: bare long-hex catch-all (32+ hex chars). Must run AFTER the
  // named passes so DSN/Bearer/keyword redactions keep their contextual
  // `[REDACTED]` label. Word-boundary anchored on both sides so a short
  // `#deadbe` CSS color (6 chars) or a git short SHA doesn't match. The
  // entropy lookaheads require at least one hex-letter AND one digit so
  // mono-character runs (`aaa...`, `111...`) used as sentinel/filler in
  // test fixtures pass through untouched — real AUDIT_SECRETs mix both.
  // 32 chars is the MD5/AES-128 threshold and stays well clear of common
  // 7/8/10-char alnum identifiers in URL paths.
  s = s.replace(
    /\b(?=[a-fA-F0-9]*[a-fA-F])(?=[a-fA-F0-9]*[0-9])[a-fA-F0-9]{32,}\b/g,
    "[REDACTED]",
  );
  // Pass 8: bare long-base64 catch-all (40+ base64 alphabet chars, up to
  // 2 `=` padding chars). Word-boundary anchored on both sides so a
  // hyphen-bearing Tailwind class (`bg-neutral-900`) or a dotted domain
  // (`docs.example.com`) doesn't match — the hyphen / dot acts as an
  // external word boundary that splits the run into sub-40 fragments.
  // Trailing boundary is `(?!\w)` rather than `\b` so the `=` padding is
  // consumed cleanly (a real `\b` would leave `==` dangling because `=`
  // is itself non-word). The entropy lookaheads require at least one
  // letter AND one digit in the run so filler sequences like
  // `"a".repeat(500)` don't redact. 40 chars is ~30 bytes of entropy,
  // well below Ed25519's 64-byte signature (88 base64 chars) and
  // comfortably above typical URL path segments and React stack-frame
  // identifiers.
  s = s.replace(
    /\b(?=[A-Za-z0-9+/]*[A-Za-z])(?=[A-Za-z0-9+/]*[0-9])[A-Za-z0-9+/]{40,}={0,2}(?!\w)/g,
    "[REDACTED]",
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
