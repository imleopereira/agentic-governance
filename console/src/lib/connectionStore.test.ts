/**
 * Tests for `sanitizeErrorMessage` — the Security-H3 helper that strips
 * common secret-bearing tokens from error messages before they land in
 * the Zustand connection store or the ErrorBoundary fallback DOM.
 *
 * The function is a heuristic, not a guarantee, so the tests both
 * verify the happy paths (Bearer, token=, password:, etc.) AND pin the
 * known limitations so a future maintainer who tightens the regex does
 * not accidentally loosen this one.
 */

import { describe, it, expect } from "vitest";
import { sanitizeErrorMessage } from "./connectionStore";

describe("sanitizeErrorMessage — secret redaction", () => {
  it("redacts a Bearer token in an Authorization header string", () => {
    const out = sanitizeErrorMessage(
      "Request failed: Authorization: Bearer sk-abc123def456",
    );
    expect(out).not.toContain("sk-abc123def456");
    expect(out.toLowerCase()).toContain("[redacted]");
  });

  it("redacts a querystring token= value", () => {
    const out = sanitizeErrorMessage("fetch failed: /api/foo?token=xyz789");
    expect(out).not.toContain("xyz789");
    expect(out.toLowerCase()).toContain("[redacted]");
  });

  it("redacts a querystring api_key= value", () => {
    const out = sanitizeErrorMessage(
      "fetch failed: /api/foo?api_key=REDACTEDKEY",
    );
    // The regex strips anything matching key=NONSPACE, so the literal
    // "REDACTEDKEY" must not survive.
    expect(out).not.toContain("REDACTEDKEY");
  });

  it("redacts a lowercase bearer header variant", () => {
    const out = sanitizeErrorMessage("authorization: bearer abc123");
    expect(out).not.toContain("abc123");
  });

  it("redacts a password: secret123 form", () => {
    const out = sanitizeErrorMessage("login failed: password: secret123");
    expect(out).not.toContain("secret123");
  });

  it("is case-insensitive across all redaction keywords", () => {
    const out = sanitizeErrorMessage(
      "BEARER xyz · TOKEN=123 · SECRET: abc · KEY=zzz",
    );
    expect(out).not.toContain("xyz");
    expect(out).not.toContain("123");
    expect(out).not.toContain("abc");
    expect(out).not.toContain("zzz");
  });
});

describe("sanitizeErrorMessage — safe inputs", () => {
  it("returns a plain error message untouched", () => {
    const out = sanitizeErrorMessage("Network unreachable");
    expect(out).toBe("Network unreachable");
  });

  it("returns empty string unchanged", () => {
    expect(sanitizeErrorMessage("")).toBe("");
  });

  it("preserves punctuation and URLs that contain no secret patterns", () => {
    const input = "GET /api/agents 500 Internal Server Error (retry 3/3)";
    expect(sanitizeErrorMessage(input)).toBe(input);
  });
});

describe("sanitizeErrorMessage — infra leak redaction (F6 item 5)", () => {
  it("redacts a Unix filesystem path under /Users", () => {
    const out = sanitizeErrorMessage(
      "ENOENT: no such file /Users/alice/secrets/key.pem",
    );
    expect(out).not.toContain("/Users/alice");
    expect(out).not.toContain("key.pem");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a Unix filesystem path under /etc", () => {
    const out = sanitizeErrorMessage("cannot read /etc/passwd: EACCES");
    expect(out).not.toContain("/etc/passwd");
    expect(out).toContain("[REDACTED]");
  });

  it("preserves a generic /api/... URL path (must not be confused with FS path)", () => {
    const out = sanitizeErrorMessage("GET /api/agents 500");
    expect(out).toContain("/api/agents");
  });

  it("redacts a Windows drive-letter path", () => {
    const out = sanitizeErrorMessage(
      "could not open C:\\Users\\bob\\config.json",
    );
    expect(out).not.toContain("bob");
    expect(out).not.toContain("config.json");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts an IPv4 address", () => {
    const out = sanitizeErrorMessage("connect ECONNREFUSED 192.168.1.42:5432");
    expect(out).not.toContain("192.168.1.42");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts an IPv6 address", () => {
    const out = sanitizeErrorMessage(
      "connect EHOSTUNREACH 2001:db8::8a2e:370:7334",
    );
    expect(out).not.toContain("2001:db8");
    expect(out).not.toContain("7334");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a postgres DSN with credentials", () => {
    const out = sanitizeErrorMessage(
      // ggignore — xkcd fixture (hunter2), not a real credential
      "could not connect: postgres://alice:hunter2@db.internal:5432/prod",
    );
    expect(out).not.toContain("alice");
    expect(out).not.toContain("hunter2");
    expect(out).not.toContain("db.internal");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a postgresql:// DSN without credentials", () => {
    const out = sanitizeErrorMessage(
      "FATAL: dial postgresql://db.example.com:5432/app",
    );
    expect(out).not.toContain("db.example.com");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a redis:// DSN", () => {
    const out = sanitizeErrorMessage("ETIMEDOUT redis://cache:6379/0");
    expect(out).not.toContain("cache:6379");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a mongodb:// DSN with credentials", () => {
    const out = sanitizeErrorMessage(
      // ggignore — test fixture, not a real credential
      "MongoServerError mongodb://root:rootpw@mongo:27017/admin",
    );
    expect(out).not.toContain("rootpw");
    expect(out).not.toContain("root:rootpw");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a mysql:// DSN", () => {
    const out = sanitizeErrorMessage(
      // ggignore — test fixture, not a real credential
      "ER_ACCESS_DENIED mysql://app:appsecret@10.0.0.5/orders",
    );
    expect(out).not.toContain("appsecret");
    expect(out).not.toContain("10.0.0.5");
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a combined message containing path + IPv4 + DSN + bearer token", () => {
    const out = sanitizeErrorMessage(
      "boot failed at /var/log/app.log peer 10.0.0.1 dsn postgres://u:p@h/d Authorization: Bearer sk-zzz",
    );
    expect(out).not.toContain("/var/log/app.log");
    expect(out).not.toContain("10.0.0.1");
    expect(out).not.toContain("postgres://");
    expect(out).not.toContain("hunter");
    expect(out).not.toContain("sk-zzz");
  });
});

describe("sanitizeErrorMessage — bare high-entropy redaction (S5 hex / base64)", () => {
  // All hex/base64 values below are obviously-fake test fixtures:
  //   ggignore — test fixture, not a real secret
  //   ggignore — test fixture, not a real secret
  const FAKE_64_HEX =
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  const FAKE_128_HEX = FAKE_64_HEX + FAKE_64_HEX;
  // 56 base64 chars with letters + digits + `+` / `/` sprinkled — shape of
  // a chopped Ed25519 signature. Trailing `==` padding is valid.
  // ggignore — test fixture, not a real signature
  const FAKE_B64_56 = "aGVsbG8rL3dvcmxkMTIzNDU2Nzg5MEFCQ0RFRkdISUpLTE1OT1BRUg==";

  it("redacts a bare 64-char hex run (fake AUDIT_SECRET)", () => {
    const out = sanitizeErrorMessage(`secret mismatch: ${FAKE_64_HEX}`);
    expect(out).not.toContain(FAKE_64_HEX);
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a bare 128-char hex run (fake HMAC digest)", () => {
    const out = sanitizeErrorMessage(`hmac check failed (${FAKE_128_HEX})`);
    expect(out).not.toContain(FAKE_128_HEX);
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a bare 32-char hex run at the boundary (MD5-length)", () => {
    // ggignore — test fixture, not a real digest
    const md5 = "0123456789abcdef0123456789abcdef";
    const out = sanitizeErrorMessage(`etag mismatch ${md5}`);
    expect(out).not.toContain(md5);
    expect(out).toContain("[REDACTED]");
  });

  it("redacts a bare ~56-char base64 run (fake Ed25519 signature)", () => {
    const out = sanitizeErrorMessage(
      `signature verification failed: ${FAKE_B64_56}`,
    );
    expect(out).not.toContain(FAKE_B64_56);
    expect(out).toContain("[REDACTED]");
  });

  it("redacts an 88-char base64 run (full Ed25519 signature length)", () => {
    // ggignore — test fixture, not a real signature
    const sig =
      "MEUCIQDTGXy1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQR+/STUVWXYZ123456abcd==";
    const out = sanitizeErrorMessage(`sig=${sig} rejected`);
    // The `sig=` keyword pass (pass 6) also strips this — either pass
    // winning is fine, the secret must not survive.
    expect(out).not.toContain(sig);
  });

  it("redacts inline hex inside a longer error sentence", () => {
    const out = sanitizeErrorMessage(
      `server echo: expected ${FAKE_64_HEX} got something else`,
    );
    expect(out).not.toContain(FAKE_64_HEX);
    expect(out).toContain("expected");
    expect(out).toContain("[REDACTED]");
  });

  // Negative cases — these MUST survive untouched so debugging context
  // is preserved and the redactor stays out of UI noise.

  it("preserves a normal URL path with short segments", () => {
    const input = "GET https://docs.example.com/api/v2/users/abc123def 404";
    const out = sanitizeErrorMessage(input);
    expect(out).toBe(input);
  });

  it("preserves a React component stack frame with line/col refs", () => {
    const input =
      "at ErrorBoundary (src/components/v4/ErrorBoundary.tsx:93:15)";
    const out = sanitizeErrorMessage(input);
    expect(out).toBe(input);
  });

  it("preserves Tailwind class names with hyphen boundaries", () => {
    const input =
      "className mismatch: bg-neutral-900 text-base font-medium rounded-md";
    const out = sanitizeErrorMessage(input);
    expect(out).toBe(input);
  });

  it("preserves a short CSS hex color like #deadbe", () => {
    const input = "invalid color #deadbe in theme token";
    const out = sanitizeErrorMessage(input);
    expect(out).toContain("#deadbe");
  });

  it("preserves short alnum identifiers in URL paths", () => {
    const input = "POST /api/agents/a1b2c3d4 500";
    const out = sanitizeErrorMessage(input);
    expect(out).toBe(input);
  });

  it("preserves mono-character filler runs (no entropy)", () => {
    // 40 `a`s — over the base64 length threshold but all-same-char, so
    // the entropy lookaheads skip it. Guarantees the existing
    // `"a".repeat(120)` identity test keeps passing.
    const input = "a".repeat(40);
    expect(sanitizeErrorMessage(input)).toBe(input);
  });

  it("still redacts a DSN with an embedded long-hex password (pass 1 wins)", () => {
    const out = sanitizeErrorMessage(
      // ggignore — test fixture, not a real credential
      `could not connect: postgres://user:${FAKE_64_HEX}@db:5432/app`,
    );
    expect(out).not.toContain(FAKE_64_HEX);
    // The DSN pass redacts the whole connection string, including the
    // long-hex password — no need for pass 7 to re-redact.
    expect(out).toContain("[REDACTED]");
  });
});

describe("sanitizeErrorMessage — truncation", () => {
  it("leaves messages at or under 120 chars unchanged", () => {
    const input = "a".repeat(120);
    expect(sanitizeErrorMessage(input)).toBe(input);
  });

  it("truncates messages longer than 120 chars to 117 chars plus ellipsis", () => {
    const input = "a".repeat(500);
    const out = sanitizeErrorMessage(input);
    expect(out.length).toBe(120);
    expect(out.endsWith("...")).toBe(true);
  });

  it("truncation happens after redaction, not before", () => {
    // 100 "a" + "bearer sk-abc123def456" + 50 "b" — the full length is
    // over 120 so it will truncate. The bearer redaction must still
    // strip the secret first; otherwise the truncation could cut in
    // the middle of the secret and leak a partial token.
    const input =
      "a".repeat(100) + " bearer sk-abc123def456 " + "b".repeat(50);
    const out = sanitizeErrorMessage(input);
    expect(out).not.toContain("sk-abc123def456");
  });
});
