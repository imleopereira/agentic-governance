import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";
import { WalkthroughProvider } from "@/components/Walkthrough";
import { AppShell } from "@/components/AppShell";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  variable: "--font-jetbrains-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
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
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`}>
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
          <WalkthroughProvider>
            <AppShell>{children}</AppShell>
          </WalkthroughProvider>
        </Providers>
      </body>
    </html>
  );
}
