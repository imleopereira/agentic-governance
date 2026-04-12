"use client";

import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { queryClient } from "@/lib/queryClient";

/**
 * Root QueryClientProvider.
 *
 * FIX 3: use the shared `queryClient` singleton from `@/lib/queryClient`
 * as the ONE client for the whole app (both v3 and v4). Previously this
 * file instantiated a component-local client via `useState`, which
 * meant:
 *   (a) logout only cleared the v4 cache — v3 queries leaked across
 *       users on the same tab,
 *   (b) the v4 layout's nested QueryClientProvider ran against a
 *       different instance from v3 routes, so posture queries could
 *       not dedupe between the two subtrees.
 *
 * A single shared client plugs (a), unifies caching for (b), and lets
 * the list query + drill query share results for free.
 */
export function Providers({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
