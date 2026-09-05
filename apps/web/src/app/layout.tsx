import type { Metadata } from "next";

import "./globals.css";

/**
 * Satoshi, served by Fontshare rather than vendored.
 *
 * A geometric grotesque carries AgentRank's voice at every size: the verdict headline at
 * ninety-odd pixels, the interface text at fourteen, and the numerals in between. Evidence stays
 * monospace, which is the one place a second family earns its keep.
 *
 * The typeface is the Indian Type Foundry's, distributed by Fontshare under the ITF Free Font
 * Licence. That licence permits using the font on a website and forbids passing the font files
 * on to anybody else, so a public repository cannot carry them. Fontshare's own stylesheet
 * endpoint serves them instead, and the system stack in globals.css stands in until it answers,
 * or for a console with no route to it.
 */
const FONTSHARE_STYLESHEET =
  "https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700,900&display=swap";

export const metadata: Metadata = {
  title: "AgentRank",
  description: "AI commerce readiness benchmark and Merchant Compiler",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://api.fontshare.com" />
        <link rel="preconnect" href="https://cdn.fontshare.com" crossOrigin="anonymous" />
        <link rel="stylesheet" href={FONTSHARE_STYLESHEET} />
      </head>
      <body>{children}</body>
    </html>
  );
}
