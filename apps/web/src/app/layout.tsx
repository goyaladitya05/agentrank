import type { Metadata } from "next";
import localFont from "next/font/local";

import "./globals.css";

/**
 * Satoshi, vendored under ./fonts with its ITF Free Font Licence.
 *
 * A geometric grotesque carries AgentRank's voice at every size: the verdict headline at
 * ninety-odd pixels, the interface text at fourteen, and the numerals in between. Loaded
 * locally so builds are deterministic and no request leaves the deployment for a typeface.
 * Evidence stays monospace, which is the one place a second family earns its keep.
 */
const satoshi = localFont({
  src: [
    { path: "./fonts/satoshi-400.woff2", weight: "400", style: "normal" },
    { path: "./fonts/satoshi-500.woff2", weight: "500", style: "normal" },
    { path: "./fonts/satoshi-700.woff2", weight: "700", style: "normal" },
    { path: "./fonts/satoshi-900.woff2", weight: "900", style: "normal" },
  ],
  display: "swap",
  variable: "--font-sans",
  fallback: ["-apple-system", "BlinkMacSystemFont", "Segoe UI", "Roboto", "sans-serif"],
});

export const metadata: Metadata = {
  title: "AgentRank",
  description: "AI commerce readiness benchmark and Merchant Compiler",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={satoshi.variable}>
      <body>{children}</body>
    </html>
  );
}
