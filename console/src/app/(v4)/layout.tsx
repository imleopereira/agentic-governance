"use client";

import type { ReactNode } from "react";
import { notFound } from "next/navigation";

import { ErrorBoundary } from "@/components/v4/ErrorBoundary";
import { DisconnectBanner } from "@/components/DisconnectBanner";
import { ComplianceHeaderPill } from "@/components/v4/ComplianceHeaderPill";

/**
 * v4 console shell layout boundary.
 *
 * Auth note: the root `app/layout.tsx` already wraps every route in
 * `<AppShell>` (which runs `AuthGate`), so no additional auth guard
 * here.
 *
 * FIX 3 (providers): the root `providers.tsx` now uses the SAME shared
 * `@/lib/queryClient` instance that this subtree used to wrap locally.
 * We therefore drop the nested `<QueryClientProvider>` — one client for
 * the whole app means logout clears everything, and v3/v4 queries
 * dedupe. This layout still contributes the v4 ErrorBoundary +
 * DisconnectBanner wrapping.
 *
 * FIX 6 (v0.6.2): v4 is now the default. Call `notFound()` only when
 * the env var is explicitly `v3` — a user who has forced v3 should not
 * render v4 pages. The unset case falls through to v4 per the flipped
 * default. Cookie-based `?ui=v3` overrides are handled by middleware.ts
 * (which redirects) and do not reach this layout.
 */
export default function V4Layout({ children }: { children: ReactNode }) {
  const version = process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION ?? "v4";

  if (version === "v3") {
    notFound();
  }

  return (
    <ErrorBoundary>
      <div className="v4-shell">
        <DisconnectBanner />
        <div
          className="v4-header-pill-slot"
          style={{
            position: "fixed",
            top: 12,
            right: 16,
            zIndex: 50,
          }}
        >
          <ComplianceHeaderPill />
        </div>
        {children}
      </div>
    </ErrorBoundary>
  );
}
