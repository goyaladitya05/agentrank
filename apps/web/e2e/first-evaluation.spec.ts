import { execFileSync } from "node:child_process";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { retainOnFailure, signIn as establishSession } from "./session";

/**
 * The Phase 4F bootstrap, end to end against real servers, a real database and a real run.
 *
 * A merchant AgentRank has never measured signs in, is told so rather than shown an empty
 * dashboard, reads what a first evaluation would measure, launches it explicitly, watches it sit
 * queued while nothing has run, and then reads the completed run and its ordinary diagnostics
 * after an operator process has executed it.
 *
 * Nothing here is mocked. The dispatcher is the real `benchmark dispatch` command in its own
 * process, which is also the point: the API never executes a benchmark, so a browser test that
 * reached a completed run without running that command would be testing something this system
 * does not do.
 *
 * The seeded merchant comes from scripts/seed_first_evaluation_e2e.py: a fresh slug, its own
 * benchmark world, a two mission suite and one source snapshot. Deliberately no compiler run and
 * no published representation, because the property under test is that a merchant needs neither
 * to get their first measurement.
 */

const key = process.env.AGENTRANK_E2E_FIRST_KEY;
const world = process.env.AGENTRANK_E2E_FIRST_WORLD;

// One real benchmark run, spawning a worker process per mission and paying through the real
// payment kernel, plus the page loads around it. Generous, and still bounded.
test.setTimeout(180_000);

test.afterEach(async ({ context }, testInfo) => {
  await retainOnFailure(context, testInfo);
});

/**
 * Run the operator dispatcher once, exactly as an operator would.
 *
 * The provider variables are emptied rather than left alone: a developer machine may have a real
 * key in `.env`, and a browser test must never spend one. With none configured the launch is
 * admitted for the deterministic reference buyer, which the console says plainly.
 */
function dispatch(): string {
  if (world === undefined) throw new Error("AGENTRANK_E2E_FIRST_WORLD is required");
  return execFileSync(
    "uv",
    ["run", "python", "-m", "agentrank_api.cli", "benchmark", "dispatch", "--world", world],
    {
      cwd: "../..",
      encoding: "utf-8",
      env: { ...process.env, OPENAI_API_KEY: "", GEMINI_API_KEY: "" },
    },
  );
}

/** The console navigation, so a link in it is never confused with one in the page body. */
function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Console" }).getByRole("link", { name });
}

/** The newest launch in the history table. */
function newestLaunch(page: Page) {
  return page.locator('[aria-label="Evaluations"] a').first();
}

async function signIn(page: Page, context: BrowserContext): Promise<void> {
  if (key === undefined) throw new Error("AGENTRANK_E2E_FIRST_KEY is required");
  await establishSession(page, context, key);
  await expect(nav(page, "Evaluation")).toBeVisible();
}

test("a merchant with no benchmark history reaches their first result from the console", async ({
  page,
  context,
}) => {
  await signIn(page, context);

  // The zero state is factual and carries the one action that changes it.
  await page.goto("/overview");
  await expect(page.getByText("No evaluations have run yet")).toBeVisible();
  await expect(page.getByRole("link", { name: "Run your first evaluation" }).first()).toBeVisible();

  // What would be evaluated, before anything is spent. No representation is named, because
  // none exists, and nothing claims there is a previous result.
  await nav(page, "Evaluation").click();
  await expect(page.getByRole("heading", { name: "Run your first evaluation" })).toBeVisible();
  await expect(
    page.getByText("Your merchant as it is now, through the ordinary storefront"),
  ).toBeVisible();
  await expect(page.getByText("no earlier result to read it against")).toBeVisible();
  await expect(page.getByText("No evaluations have run yet")).toBeVisible();

  // Launching is an explicit second act, and the confirmation says what it does and does not do.
  await page.getByRole("button", { name: "Review first evaluation" }).click();
  const form = page.getByRole("form", { name: "Confirm first evaluation" });
  await expect(form).toContainText("2 missions are executed");
  await expect(form).toContainText("The buyer reads the ordinary storefront");
  await expect(form).toContainText("This creates your first benchmark result");
  await expect(form).toContainText("does not change your prices, inventory or any payment");
  await form.getByRole("button", { name: "Request first evaluation" }).click();

  // Queued is an honest state: the browser request admitted work and executed none of it.
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();
  await newestLaunch(page).click();
  await expect(page.getByRole("heading", { name: "First evaluation" })).toBeVisible();
  await expect(page.getByText("Nothing has been executed yet")).toBeVisible();
  await expect(page.getByText("no model quota has been spent")).toBeVisible();
  await expect(page.getByText("Open the benchmark run")).toHaveCount(0);
  // A first evaluation has no before, so the page has no comparison section to be empty.
  await expect(page.getByText("Compared with your previous run")).toHaveCount(0);
  await expect(page.getByText("No comparison yet")).toHaveCount(0);

  // The API executes nothing. An operator process claims the launch and runs it.
  expect(dispatch()).toContain("COMPLETED");

  await page.reload();
  await expect(page.getByText("2 of 2 missions finished")).toBeVisible();
  // Still no comparison, and still nothing standing in for one.
  await expect(page.getByText("Compared with your previous run")).toHaveCount(0);
  await expect(page.getByText("0%")).toHaveCount(0);

  // The next legitimate step is ordinary product navigation, not a causal claim.
  await expect(page.getByRole("link", { name: "Review your merchant source" })).toBeVisible();
  await expect(page.getByText("Review the compiler facts")).toHaveCount(0);

  // The completed run reaches the ordinary diagnostics surfaces, with no special first-run path.
  await page.getByRole("link", { name: "Open the benchmark run" }).click();
  await expect(page.getByRole("heading", { name: "Run detail" })).toBeVisible();
  await expect(page.getByText("Task completion")).toBeVisible();

  // And the merchant's history is exactly one evaluation and one run. No synthetic before was
  // written to give the first result something to sit beside.
  await nav(page, "Evaluation").click();
  await expect(page.locator('[aria-label="Evaluations"] tbody tr')).toHaveCount(1);
  await expect(page.getByText("First evaluation").first()).toBeVisible();
  await nav(page, "Runs").click();
  await expect(page.locator("table tbody tr")).toHaveCount(1);
});
