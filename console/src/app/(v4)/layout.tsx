"use client";

import type { ReactNode } from "react";
import { notFound } from "next/navigation";

import { ErrorBoundary } from "@/components/v4/ErrorBoundary";
import { DisconnectBanner } from "@/components/v4/DisconnectBanner";

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
 * FIX 6: when `NEXT_PUBLIC_CONSOLE_UI_VERSION` is not `v4`, call
 * `notFound()` so the route hits the 404 page instead of rendering an
 * empty screen for a hand-typed v4 URL under v3.
 */
export default function V4Layout({ children }: { children: ReactNode }) {
  const version = process.env.NEXT_PUBLIC_CONSOLE_UI_VERSION ?? "v3";

  if (version !== "v4") {
    notFound();
  }

  return (
    <ErrorBoundary>
      <div className="v4-shell">
        <DisconnectBanner />
        {children}
      </div>
    </ErrorBoundary>
  );
}
