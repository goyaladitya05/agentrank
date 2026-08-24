import { defineConfig } from "@playwright/test";

/**
 * The critical browser workflow runs against real servers and a real database.
 *
 * Both servers are started here on ports the development ones do not use, and neither is reused:
 * a stale process holding 3001 would silently test yesterday's build. One worker and no retries,
 * because the run is seeded with one merchant whose workflow ends in an immutable publication,
 * and a retry of a publication that already happened is not the same test.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  forbidOnly: process.env.CI !== undefined,
  reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:3001", browserName: "chromium", trace: "retain-on-failure" },
  webServer: [
    {
      command: "uv run uvicorn agentrank_api.main:create_app --factory --port 8001",
      cwd: "../..",
      url: "http://127.0.0.1:8001/health",
      reuseExistingServer: false,
    },
    {
      command: "next start --port 3001",
      cwd: ".",
      env: { AGENTRANK_API_BASE_URL: "http://127.0.0.1:8001" },
      url: "http://127.0.0.1:3001/login",
      reuseExistingServer: false,
    },
  ],
});
