import { execFileSync } from "node:child_process";

import { expect, test, type Page } from "@playwright/test";

/**
 * The Phase 4D product loop, end to end against real servers, a real database and real runs.
 *
 * A merchant publishes an agent-ready representation, sees for themselves that publishing
 * started nothing, explicitly requests a re-evaluation, watches it sit queued while no benchmark
 * has run, and then reads the completed run and its comparison after an operator process has
 * executed it.
 *
 * Nothing here is mocked. The dispatcher is the real `benchmark reevaluate` command in its own
 * process, which is also the point: the API never executes a benchmark, so a browser test that
 * reached a completed run without running that command would be testing something this system
 * does not do.
 *
 * The seeded merchant comes from scripts/seed_reevaluation_e2e.py: a fresh slug, its own
 * benchmark world, a two mission suite and one compiler run whose facts need no decision. The
 * world is trimmed to two missions because a browser test that waits for fourteen real missions
 * is a browser test nobody runs.
 */

const key = process.env.AGENTRANK_E2E_REEVALUATION_KEY;
const world = process.env.AGENTRANK_E2E_REEVALUATION_WORLD;

// Two real benchmark runs, each spawning a worker process per mission and paying through the
// real payment kernel, plus two page loads around each. Generous, and still bounded.
test.setTimeout(240_000);

/**
 * Run the operator dispatcher once, exactly as an operator would.
 *
 * The provider variables are emptied rather than left alone: a developer machine may have a
 * real key in `.env`, and a browser test must never spend one. With none configured the launch
 * is admitted for the deterministic reference buyer, which the console says plainly.
 */
function dispatch(): string {
  if (world === undefined) throw new Error("AGENTRANK_E2E_REEVALUATION_WORLD is required");
  return execFileSync(
    "uv",
    ["run", "python", "-m", "agentrank_api.cli", "benchmark", "reevaluate", "--world", world],
    {
      cwd: "../..",
      encoding: "utf-8",
      env: { ...process.env, OPENAI_API_KEY: "", GEMINI_API_KEY: "" },
    },
  );
}

interface CapturedAction {
  url: string;
  headers: Record<string, string>;
  body: string;
}

/**
 * Record the real launch the console sends, so it can be replayed verbatim.
 *
 * Scoped to the re-evaluation pages: sign in is a server action too, and replaying that one
 * would be replaying a merchant API key rather than a benchmark write.
 */
function captureLaunch(page: Page, captured: CapturedAction[]): void {
  page.on("request", (request) => {
    const headers = request.headers();
    if (request.method() !== "POST" || headers["next-action"] === undefined) return;
    if (!request.url().includes("/re-evaluations")) return;
    if (captured.length > 0) return;
    const replay: Record<string, string> = {};
    for (const [name, value] of Object.entries(headers)) {
      if (name !== "origin" && name !== "host" && name !== "cookie" && !name.startsWith(":")) {
        replay[name] = value;
      }
    }
    captured.push({ url: request.url(), headers: replay, body: request.postData() ?? "" });
  });
}

/** The console navigation, so a link in it is never confused with one in the page body. */
function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Console" }).getByRole("link", { name });
}

/** The newest launch in the history table. */
function newestLaunch(page: Page) {
  return page.locator('[aria-label="Re-evaluations"] a').first();
}

async function signIn(page: Page): Promise<void> {
  if (key === undefined) throw new Error("AGENTRANK_E2E_REEVALUATION_KEY is required");
  await page.goto("/login");
  await page.getByLabel("Merchant API key").fill(key);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(nav(page, "Re-evaluation")).toBeVisible();
}

async function publishRepresentation(page: Page): Promise<void> {
  await nav(page, "Compiler").click();
  await page.getByRole("link", { name: /-source@1/ }).click();
  await expect(page.getByRole("heading", { name: "Review compiler run" })).toBeVisible();
  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByText("does not rerun a benchmark")).toBeVisible();
  await page.getByRole("button", { name: "Publish representation" }).click();
  await expect(page.getByText("Agent-ready representation published:")).toBeVisible();
}

async function requestReevaluation(page: Page): Promise<void> {
  await nav(page, "Re-evaluation").click();
  await page.getByRole("button", { name: "Review re-evaluation" }).click();
  const form = page.getByRole("form", { name: "Confirm re-evaluation" });
  await expect(form).toContainText("2 missions are executed");
  await expect(form).toContainText("Every previous run and its findings stay exactly as they are");
  await form.getByRole("button", { name: "Request re-evaluation" }).click();
}

test("a merchant publishes, launches a re-evaluation and reads the result against the run before it", async ({
  page,
}) => {
  const captured: CapturedAction[] = [];
  captureLaunch(page, captured);
  await signIn(page);
  await publishRepresentation(page);

  // Publishing said so itself, and the launch history proves it: nothing was started.
  await expect(page.getByText("Publishing did not run a benchmark")).toBeVisible();
  await nav(page, "Re-evaluation").click();
  await expect(page.getByText("No re-evaluations yet")).toBeVisible();

  await requestReevaluation(page);

  // Queued is an honest state and says exactly what has and has not happened. A second launch
  // is refused while it is pending, which the preflight says in place.
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();
  await expect(page.getByText("A re-evaluation is already queued or running")).toBeVisible();
  await newestLaunch(page).click();
  await expect(page.getByText("Nothing has been executed yet")).toBeVisible();
  await expect(page.getByText("no model quota has been spent")).toBeVisible();
  await expect(page.getByText("Open the benchmark run")).toHaveCount(0);

  // The API executes nothing. An operator process claims the launch and runs it.
  expect(dispatch()).toContain("COMPLETED");

  await page.reload();
  await expect(page.getByText("2 of 2 missions finished")).toBeVisible();
  await expect(page.getByText("no earlier completed run of this suite")).toBeVisible();

  // The completed run reaches the ordinary diagnostics surfaces.
  await page.getByRole("link", { name: "Open the benchmark run" }).click();
  await expect(page.getByRole("heading", { name: "Run detail" })).toBeVisible();
  await expect(page.getByText("Task completion")).toBeVisible();

  // A second re-evaluation is compared with the first, with its caveats attached.
  await requestReevaluation(page);
  expect(dispatch()).toContain("COMPLETED");
  await newestLaunch(page).click();
  await expect(page.getByText("2 of 2 missions finished")).toBeVisible();
  await expect(page.getByText("Not a controlled experiment", { exact: true })).toBeVisible();
  await expect(page.getByText("One run on each side", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "before", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "after", exact: true })).toBeVisible();

  // The same signed in browser, the same recorded launch, and only the origin changed. One is
  // executed and answered; the other never reaches the action at all. Neither produces a third
  // launch: the request key the recorded body carries is one this merchant has already used, so
  // a replay is the launch it already made rather than a new benchmark run.
  const action = captured[0];
  expect(action).toBeDefined();
  if (action === undefined) return;
  const sameOrigin = await page.request.post(action.url, {
    headers: { ...action.headers, origin: "http://127.0.0.1:3001" },
    data: action.body,
  });
  expect(sameOrigin.status()).toBe(200);
  const crossOrigin = await page.request.post(action.url, {
    headers: { ...action.headers, origin: "https://attacker.example" },
    data: action.body,
  });
  expect(crossOrigin.status()).not.toBe(200);

  await nav(page, "Re-evaluation").click();
  await expect(page.locator('[aria-label="Re-evaluations"] tbody tr')).toHaveCount(2);
});

test("the console shows nothing about a re-evaluation without a signed in session", async ({
  page,
}) => {
  await page.context().clearCookies();
  await page.goto("/re-evaluations");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByLabel("Merchant API key")).toBeVisible();
});
