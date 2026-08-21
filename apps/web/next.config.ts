import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The console is an internal tool. Advertising the framework version buys nothing.
  poweredByHeader: false,
};

export default config;
