import { spawn, type ChildProcess } from "node:child_process";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { record, retainOnFailure, signIn } from "./session";

/**
 * The promise Phase 5A makes about browser sessions, proved against a real second process.
 *
 * The console used to hold sessions in the memory of whichever Next.js process minted one, so a
 * cookie identified a session only to that process. This starts a second console, with its own
 * empty memory, against the same API and the same deployment secret, and drives the same browser
 * at it. A second process serving a session it never saw opened is the same proof a restart
 * would give, because a freshly started process has nothing in memory either.
 *
 * Cookies are not scoped by port, so the browser sends the same cookie to both consoles without
 * anything here copying it. That is the honest version of the test: nothing is transplanted, the
 * browser simply makes its next request to a different server.
 *
 * The last step is the one that makes the rest mean something. Signing out on the second console
 * has to end the session on the first, because revocation that was local to a process would be
 * the old defect wearing the new architecture.
 */

const key = process.env.AGENTRANK_E2E_KEY;
const secret = process.env.AGENTRANK_CONSOLE_SESSION_SECRET;

const SECOND_PORT = 3002;
const SECOND_ORIGIN = `http://127.0.0.1:${String(SECOND_PORT)}`;
const API_ORIGIN = "http://127.0.0.1:8001";

const BOOT_TIMEOUT_MS = 60_000;
const BOOT_POLL_MS = 250;

let second: ChildProcess | undefined;

/**
 * Where this console's build and its installed binaries are.
 *
 * The working directory, because that is what the package script this suite runs under sets and
 * what `playwright.config.ts` already relies on for its own servers. Specs are transpiled to
 * CommonJS by the Playwright runner, so neither `import.meta.dirname` nor `__dirname` is a
 * portable answer here.
 */
const CONSOLE_ROOT = process.cwd();

function pause(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForConsole(origin: string): Promise<void> {
  const deadline = Date.now() + BOOT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${origin}/api/health`);
      if (response.ok) {
        return;
      }
    } catch {
      // Not listening yet. The deadline is the bound; a failure to connect is expected here.
    }
    await pause(BOOT_POLL_MS);
  }
  throw new Error(`the second console did not become healthy at ${origin}`);
}

test.beforeAll(async () => {
  if (secret === undefined) {
    throw new Error("AGENTRANK_CONSOLE_SESSION_SECRET is required to start a second console");
  }
  // The installed binary by path rather than by name. A test worker does not inherit the PATH a
  // package script would have, so "next" alone resolves on a developer machine and not in CI.
  const nextBinary = join(CONSOLE_ROOT, "node_modules", ".bin", "next");
  second = spawn(nextBinary, ["start", "--port", String(SECOND_PORT)], {
    // The same deployment secret and the same API. Everything else is a fresh process with an
    // empty heap, which is the whole point.
    env: {
      ...process.env,
      AGENTRANK_API_BASE_URL: API_ORIGIN,
      AGENTRANK_CONSOLE_SESSION_SECRET: secret,
      AGENTRANK_COOKIE_SECURE: "false",
    },
    cwd: CONSOLE_ROOT,
    stdio: "ignore",
    shell: false,
  });
  await waitForConsole(SECOND_ORIGIN);
});

test.afterAll(() => {
  second?.kill("SIGTERM");
});

test("a console session outlives the process that issued it and is revoked everywhere", async ({
  browser,
  baseURL,
}, testInfo) => {
  if (key === undefined) throw new Error("AGENTRANK_E2E_KEY is required");
  const context = await browser.newContext(baseURL === undefined ? {} : { baseURL });
  const page = await context.newPage();

  await signIn(page, context, key);
  await expect(page.getByRole("navigation", { name: "Console" })).toBeVisible();

  // The session as this console sees it. Nothing about it is read from memory: the cookie is
  // opaque and the record it names lives in PostgreSQL.
  const cookies = await context.cookies();
  const session = cookies.find((entry) => entry.name === "ar_console_session");
  expect(session, "signing in must set the console session cookie").toBeDefined();
  expect(session?.httpOnly, "the cookie must be unreadable from client JavaScript").toBe(true);
  expect(session?.value.startsWith("arc_"), "the cookie is not the API credential").toBe(true);

  // A different process, with an empty heap, that never saw this session opened.
  await page.goto(`${SECOND_ORIGIN}/overview`);
  await expect(page.getByRole("navigation", { name: "Console" })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`^${SECOND_ORIGIN}/overview`));

  // And it serves an ordinary merchant screen rather than only rendering the shell.
  await page
    .getByRole("navigation", { name: "Console" })
    .getByRole("link", { name: "Compiler" })
    .click();
  await expect(page.getByRole("heading", { name: "Compiler review" })).toBeVisible();

  // Signing out on the second console. If revocation were process local this would leave the
  // first console still serving the session, which is exactly the defect being fixed.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await page.goto("/overview");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("navigation", { name: "Console" })).toHaveCount(0);

  await retainOnFailure(context, testInfo);
  await context.close();
});

test("a cookie the deployment secret cannot resolve is not a session", async ({
  browser,
  baseURL,
}, testInfo) => {
  /**
   * The other half of the trust decision. A cookie is not the credential the API knows about, so
   * a value that looks like one but was not derived under this deployment's secret authenticates
   * nothing, on any console process.
   */
  const context = await browser.newContext(baseURL === undefined ? {} : { baseURL });
  await record(context);
  const page = await context.newPage();

  await context.addCookies([
    {
      name: "ar_console_session",
      value: `arc_${"e".repeat(64)}`,
      domain: "127.0.0.1",
      path: "/",
      httpOnly: true,
      secure: false,
      sameSite: "Lax",
    },
  ]);

  // Both consoles answer the same way, and both say why rather than rendering a signed in shell
  // full of refusals. A cookie being present stopped being the same question as a session being
  // open once the record moved to the API, so the console asks and acts on the answer.
  await page.goto("/overview");
  await expect(page).toHaveURL(/\/login\?error=expired$/);
  // Scoped to the page's own main region: Next.js renders a route announcer with the same role,
  // and an unscoped locator matches both.
  await expect(page.getByRole("main").getByRole("alert")).toContainText("session has ended");

  await page.goto(`${SECOND_ORIGIN}/overview`);
  await expect(page).toHaveURL(new RegExp(`^${SECOND_ORIGIN}/login\\?error=expired$`));

  await retainOnFailure(context, testInfo);
  await context.close();
});
