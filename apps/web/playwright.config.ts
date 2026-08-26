import { randomBytes } from "node:crypto";

import { defineConfig } from "@playwright/test";

/**
 * The console session secret this run's console is started with.
 *
 * Generated here rather than written down anywhere. Nothing outside this process tree has ever
 * seen it, so a session cookie captured in a retained trace names a credential that cannot be
 * derived again once the run is over.
 *
 * Placed on this process' own environment as well as the server's, because the durability spec
 * starts a second console and has to start it with the same secret: two consoles with different
 * secrets would derive different credentials from one cookie, which is a misconfiguration rather
 * than the property under test. Test workers are children of this process and inherit it.
 */
process.env.AGENTRANK_CONSOLE_SESSION_SECRET ??= randomBytes(32).toString("hex");
const SESSION_SECRET = process.env.AGENTRANK_CONSOLE_SESSION_SECRET;

/**
 * The synthetic merchant storefront the import workflow reads from.
 *
 * A port this repository chose rather than an ephemeral one, because the API process has to be
 * told which networks it may reach before either process starts, and the spec has to be able to
 * type a URL into a form.
 */
const STOREFRONT_PORT = "8002";

/**
 * The critical browser workflow runs against real servers and a real database.
 *
 * Both servers are started here on ports the development ones do not use, and neither is reused:
 * a stale process holding 3001 would silently test yesterday's build. One worker and no retries,
 * because the run is seeded with one merchant whose workflow ends in an immutable publication,
 * and a retry of a publication that already happened is not the same test.
 *
 * `trace` is off here and is not off in practice. A trace records the arguments of every action,
 * the DOM around it and the network it caused, so a configured trace starts before the merchant
 * API key is typed into the sign in form and retains it on any later failure. Every test starts
 * its own trace instead, after sign in, through `e2e/session.ts`, and writes it out under exactly
 * the retain-on-failure rule this option would have applied. Diagnostics are unchanged for
 * everything except the two steps that carry a credential.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  forbidOnly: process.env.CI !== undefined,
  reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:3001", browserName: "chromium", trace: "off" },
  webServer: [
    {
      command: "uv run uvicorn agentrank_api.main:create_app --factory --port 8001",
      cwd: "../..",
      // Provider credentials are emptied rather than inherited. A developer machine may have a
      // real key in `.env`, and a browser test must never spend one; with none configured the
      // launch is admitted for the deterministic reference buyer, which the console says.
      //
      // The merchant page importer is separately permitted to reach
      // loopback so that it can read the synthetic storefront below. `Settings` refuses that
      // second variable in any environment that is not development, CI or test, which is what
      // keeps this a local test allowance rather than a deployment setting.
      env: {
        OPENAI_API_KEY: "",
        GEMINI_API_KEY: "",
        AGENTRANK_IMPORT_ALLOWED_NETWORKS: "127.0.0.0/8",
      },
      url: "http://127.0.0.1:8001/health",
      reuseExistingServer: false,
    },
    {
      // An ordinary web server with five invented pages on it. Separate from the API on purpose:
      // a fixture served by the application under test would be one the application could special
      // case, and the property being tested is that AgentRank fetches a real socket.
      command: `uv run python scripts/serve_import_fixture.py --port ${STOREFRONT_PORT}`,
      cwd: "../..",
      url: `http://127.0.0.1:${STOREFRONT_PORT}/health`,
      reuseExistingServer: false,
    },
    {
      command: "next start --port 3001",
      cwd: ".",
      // The session secret is generated per run and lives only in this process tree. A console
      // session cookie left in a retained trace is therefore inert the moment the run ends:
      // resolving one needs this value, and nothing writes it down. `AGENTRANK_COOKIE_SECURE`
      // is the documented local HTTP exception, and these servers are plain HTTP on loopback.
      env: {
        AGENTRANK_API_BASE_URL: "http://127.0.0.1:8001",
        AGENTRANK_CONSOLE_SESSION_SECRET: SESSION_SECRET,
        AGENTRANK_COOKIE_SECURE: "false",
      },
      url: "http://127.0.0.1:3001/login",
      reuseExistingServer: false,
    },
  ],
});
