/**
 * Resilient TanStack Query client for the v4 governance console.
 *
 * CTO invariant #1: the console MUST continue rendering last-known state
 * when `/api/posture`, `/api/events`, or any governance endpoint 5xxs.
 * A disconnect banner alone is not sufficient — the UI must show CACHED
 * data, not a blank screen.
 *
 * Mechanism:
 *   - staleTime: Infinity — cached data is never considered stale, so a
 *     failed refetch does not evict it from the UI.
 *   - gcTime: 1h — keeps data resident long enough for operator workflows.
 *   - retry: 3 with exponential backoff (2s / 4s / 8s, capped 30s).
 *   - A global QueryCache onError handler updates useConnectionStore so
 *     the shell can render the v4 DisconnectBanner over the cached UI.
 *   - onSuccess clears the error state and returns us to "ok".
 */

import { QueryCache, QueryClient } from "@tanstack/react-query";

import { useConnectionStore } from "./connectionStore";

const ONE_HOUR_MS = 1000 * 60 * 60;

const queryCache = new QueryCache({
  onError: (error) => {
    const err = error instanceof Error ? error : new Error(String(error));
    useConnectionStore.getState().reportError(err);
  },
  onSuccess: () => {
    useConnectionStore.getState().reportSuccess();
  },
});

export const queryClient: QueryClient = new QueryClient({
  queryCache,
  defaultOptions: {
    queries: {
      staleTime: Infinity,
      gcTime: ONE_HOUR_MS,
      // throwOnError: false (default). Per-panel error branches render
      // their own sanitized messages via `useQuery().error`.
      // ErrorBoundary catches only render-time exceptions, not query
      // errors — this is the intentional scope. The QueryCache.onError
      // hook above still pushes health into connectionStore so the
      // DisconnectBanner lights up on network trouble.
      retry: 3,
      retryDelay: (attemptIndex: number) =>
        Math.min(2000 * 2 ** attemptIndex, 30000),
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
    },
    mutations: {
      retry: 0,
    },
  },
});
