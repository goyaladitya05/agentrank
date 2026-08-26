import type { Metadata } from "next";
import localFont from "next/font/local";

import "./globals.css";

/**
 * Fraunces, AgentRank's editorial display voice, vendored under ./fonts with its SIL Open
 * Font License. A variable weight file, latin subset only, loaded locally so builds are
 * deterministic and no request ever leaves the deployment for a typeface. Headlines and
 * the numbers that carry a verdict use it; interface text stays on the system sans and
 * evidence stays monospace.
 */
const fraunces = localFont({
  src: "./fonts/fraunces-latin-var.woff2",
  weight: "400 700",
  display: "swap",
  variable: "--font-serif",
  fallback: ["Georgia", "Times New Roman", "serif"],
  adjustFontFallback: false,
});

export const metadata: Metadata = {
  title: "AgentRank",
  description: "AI commerce readiness benchmark and Merchant Compiler",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={fraunces.variable}>
      <body>{children}</body>
    </html>
  );
}
