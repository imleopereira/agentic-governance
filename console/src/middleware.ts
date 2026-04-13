import { NextResponse, type NextRequest } from "next/server";

/**
 * v3 / v4 console routing.
 *
 * - `CONSOLE_UI_VERSION` (exposed as `NEXT_PUBLIC_CONSOLE_UI_VERSION`) selects
 *   the default IA. `v3` is the legacy console; `v4` is the new agents-first IA.
 * - A per-request `?ui=v4` / `?ui=v3` query string override is honored, BUT
 *   the matcher below is `["/"]` only, so the override is root-only and
 *   non-persistent: it applies only when landing on `/` and does not survive
 *   a subsequent navigation. To make it persistent across navigations, the
 *   middleware would need to set a cookie and widen the matcher. Out of
 *   scope for this seam patch — tracked as a follow-up.
 * - The root path `/` is rewritten (NOT redirected — preserves the URL) to the
 *   landing route for the active version: `/agents` for v4, `/events` for v3.
 */
export function middleware(req: NextRequest) {
  const url = req.nextUrl;
  const envVersion =
    process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION === "v4" ? "v4" : "v3";
  const override = url.searchParams.get("ui");
  const version: "v3" | "v4" =
    override === "v4" ? "v4" : override === "v3" ? "v3" : envVersion;

  if (url.pathname === "/" && version === "v4") {
    const target = url.clone();
    target.pathname = "/agents";
    return NextResponse.rewrite(target);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/"],
};
