import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "AgentRank",
  description: "AI commerce readiness benchmark and Merchant Compiler",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
