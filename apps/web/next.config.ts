import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The console is an internal tool. Advertising the framework version buys nothing.
  poweredByHeader: false,
  // Next 16 writes AI agent rule files into the repository on every dev start unless
  // told not to. This repository does not want generated files in its working tree.
  agentRules: false,
  // The Phase 6B information architecture moved the technical surfaces under /lab and
  // reframed compiler review as the merchant Fixes workflow. Old addresses keep working:
  // a bookmarked run or a link in an old operator note lands on the same page's new home.
  async redirects() {
    return [
      { source: "/runs", destination: "/lab/runs", permanent: false },
      { source: "/runs/:path*", destination: "/lab/runs/:path*", permanent: false },
      { source: "/status", destination: "/lab/status", permanent: false },
      {
        source: "/experiments/:experimentId",
        destination: "/lab/experiments/:experimentId",
        permanent: false,
      },
      { source: "/compiler", destination: "/fixes", permanent: false },
      { source: "/compiler/runs/:runId", destination: "/fixes/:runId", permanent: false },
    ];
  },
};

export default config;
