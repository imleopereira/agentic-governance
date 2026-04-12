"use client";
/**
 * AppShell - replaces top NavBar with collapsible sidebar layout.
 * Mounts the SSE hook once for the entire authenticated session.
 */

import { AuthProvider, useAuth } from "@/lib/auth";
import { LoginPage } from "@/components/LoginPage";
import { Sidebar } from "@/components/Sidebar";
import { DisconnectBanner } from "@/components/DisconnectBanner";
import { useEventStream } from "@/hooks/useEventStream";

/** Inner shell rendered only when authenticated */
function AuthenticatedShell({ children }: { children: React.ReactNode }) {
  const { manualReconnect } = useEventStream();

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      <Sidebar />
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          minWidth: 0,
        }}
      >
        <DisconnectBanner onReconnect={manualReconnect} />
        <main
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "1.5rem 2rem",
          }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}

function AuthGate({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div
        className="min-h-screen flex items-center justify-center"
        aria-label="Loading"
        aria-busy="true"
      >
        <div className="text-center space-y-3 animate-fade-in-up">
          <div
            className="w-8 h-8 border-2 border-t-transparent rounded-full animate-spin mx-auto"
            style={{ borderColor: "var(--accent)", borderTopColor: "transparent" }}
            role="status"
            aria-label="Connecting to governance API"
          />
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            Connecting to governance API&hellip;
          </p>
        </div>
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  return <AuthenticatedShell>{children}</AuthenticatedShell>;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <AuthGate>{children}</AuthGate>
    </AuthProvider>
  );
}
