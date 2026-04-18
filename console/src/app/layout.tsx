import type { Metadata } from "next";
import { Inter, IBM_Plex_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { WalkthroughProvider } from "@/components/Walkthrough";
import { AppShell } from "@/components/AppShell";
import { V3DeprecationBanner } from "@/components/V3DeprecationBanner";
import { TierProvider } from "@/lib/tierContext";
import { TierSwitcher } from "@/components/TierSwitcher";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  display: "swap",
});

const ibmPlexMono = IBM_Plex_Mono({
  variable: "--font-ibm-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Governance Console - Code Atelier",
  description:
    "Enforcement gates dashboard for AI agent governance. Monitor scope, cost, audit trails, and human-in-the-loop approval gates in real time.",
  metadataBase: new URL("https://codeatelier.tech"),
  openGraph: {
    title: "Governance Console - Code Atelier",
    description:
      "Enforcement gates dashboard for AI agent governance.",
    type: "website",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`${inter.variable} ${ibmPlexMono.variable}`}>
      <head>
        <meta name="theme-color" content="#0D0F14" />
        <meta name="color-scheme" content="dark" />
        <meta
          name="viewport"
          content="width=device-width, initial-scale=1, viewport-fit=cover"
        />
      </head>
      <body className="min-h-screen antialiased">
        <Providers>
          <TierProvider>
            <WalkthroughProvider>
              <V3DeprecationBanner />
              <AppShell>{children}</AppShell>
              <TierSwitcher />
            </WalkthroughProvider>
          </TierProvider>
        </Providers>
      </body>
    </html>
  );
}
