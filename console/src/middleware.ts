import { NextResponse, type NextRequest } from "next/server";

/**
 * v3 / v4 console routing.
 *
 * - `CONSOLE_UI_VERSION` (exposed as `NEXT_PUBLIC_CONSOLE_UI_VERSION`) selects
 *   the default IA. `v4` is the new agents-first IA and the v0.6.2 default;
 *   `v3` is the legacy console, available via explicit opt-out.
 * - A per-request `?ui=v3` / `?ui=v4` query string override is honored and
 *   PERSISTED in the `console_ui_version` cookie (SameSite=Lax, path=/, 30d).
 *   The query param is stripped on the response redirect so users see clean
 *   URLs after the first navigation.
 * - Precedence (highest to lowest): query param → cookie → env var → default `v4`.
 *   A user who forces `v3` via cookie or env beats the new default; a user who
 *   clicks a `?ui=v4` link also flips the cookie back to `v4`.
 * - The root path `/` is rewritten (NOT redirected — preserves the URL) to the
 *   landing route for the active version: `/agents` for v4, `/events` for v3.
 * - The matcher excludes `/api/*`, `/_next/*`, and static assets so middleware
 *   does not intercept backend proxy requests or Next's own asset traffic.
 */
const COOKIE_NAME = "console_ui_version";
const COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30; // 30 days

export function middleware(req: NextRequest) {
  const url = req.nextUrl;
  const envVersion: "v3" | "v4" =
    process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION === "v3" ? "v3" : "v4";

  const rawOverride = url.searchParams.get("ui");
  const override: "v3" | "v4" | null =
    rawOverride === "v3" ? "v3" : rawOverride === "v4" ? "v4" : null;

  const cookieRaw = req.cookies.get(COOKIE_NAME)?.value;
  const cookieVersion: "v3" | "v4" | null =
    cookieRaw === "v3" ? "v3" : cookieRaw === "v4" ? "v4" : null;

  const version: "v3" | "v4" = override ?? cookieVersion ?? envVersion;

  // If an override was supplied, persist it via cookie and strip the `?ui=`
  // query param so the visible URL stays clean.
  if (override !== null) {
    const clean = url.clone();
    clean.searchParams.delete("ui");
    const response = NextResponse.redirect(clean);
    response.cookies.set({
      name: COOKIE_NAME,
      value: override,
      path: "/",
      sameSite: "lax",
      maxAge: COOKIE_MAX_AGE_SECONDS,
    });
    return response;
  }

  if (url.pathname === "/" && version === "v4") {
    const target = url.clone();
    target.pathname = "/agents";
    return NextResponse.rewrite(target);
  }

  return NextResponse.next();
}

/**
 * Match everything except:
 * - `/api/*`          — console backend proxy (CRITICAL: do not intercept)
 * - `/_next/*`        — Next.js internals and static bundles
 * - `/favicon.ico`    — root favicon
 * - any path that looks like a static asset (ends in a dotted extension)
 *
 * Widened from `["/"]` in v0.6.2 so the `?ui=` override persists via cookie
 * across navigations, not only on the root landing.
 */
export const config = {
  matcher: ["/((?!api|_next|favicon.ico|.*\\..*).*)"],
};
