import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The console is an internal tool. Advertising the framework version buys nothing.
  poweredByHeader: false,
  // Next 16 writes AI agent rule files into the repository on every dev start unless
  // told not to. This repository does not want generated files in its working tree.
  agentRules: false,
};

export default config;
