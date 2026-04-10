import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";
import { WalkthroughProvider } from "@/components/Walkthrough";

export const metadata: Metadata = {
  title: "Governance Console — Code Atelier",
  description: "Enforcement gates dashboard for AI agent governance",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <Providers>
          <WalkthroughProvider>
            <nav className="border-b border-[var(--border)] px-6 py-3 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <span className="font-bold text-lg">Governance Console</span>
                <span className="text-xs text-[var(--muted)]">v0.2.0</span>
              </div>
              <div className="flex gap-4 text-sm">
                <a
                  href="/"
                  className="hover:text-[var(--accent)]"
                  data-tour="posture"
                >
                  Posture
                </a>
                <a
                  href="/events"
                  className="hover:text-[var(--accent)]"
                  data-tour="nav-events"
                >
                  Audit Log
                </a>
                <a
                  href="/cost"
                  className="hover:text-[var(--accent)]"
                  data-tour="nav-cost"
                >
                  Cost
                </a>
                <a
                  href="/gates"
                  className="hover:text-[var(--accent)]"
                  data-tour="nav-gates"
                >
                  Gates
                </a>
              </div>
            </nav>
            <main className="max-w-7xl mx-auto px-6 py-8">{children}</main>
          </WalkthroughProvider>
        </Providers>
      </body>
    </html>
  );
}
