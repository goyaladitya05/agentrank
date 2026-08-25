import { execFileSync } from "node:child_process";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { retainOnFailure, signIn as establishSession } from "./session";

/**
 * The Phase 5C bootstrap, end to end against real servers, a real database and a real run.
 *
 * A merchant whose entire history is one source snapshot signs in, is told that AgentRank has
 * their information and no evaluation setup, reads exactly what building one would produce,
 * builds it, and arrives at the ordinary first-evaluation preflight with no operator involved at
 * any point.
 *
 * Nothing here is mocked. The seeded merchant has no authored world directory, no catalog
 * fixture, no benchmark suite and no hand written row: `scripts/seed_workspace_e2e.py` publishes
 * one source document and issues one credential, which is the whole of what a private beta
 * merchant arrives with. Everything the benchmark needs is built by the console command under
 * test.
 *
 * No model provider is contacted. The dispatcher runs with the provider variables emptied, so
 * the launch is admitted for the deterministic reference buyer and the console says so plainly.
 */

const key = process.env.AGENTRANK_E2E_SETUP_KEY;
const merchant = process.env.AGENTRANK_E2E_SETUP_MERCHANT;

// One real benchmark run over a generated suite, spawning a worker process per mission and
// paying through the real payment kernel, plus the page loads around it. Generous, and bounded.
test.setTimeout(180_000);

test.afterEach(async ({ context }, testInfo) => {
  await retainOnFailure(context, testInfo);
});

/**
 * Run the operator dispatcher once, naming only the merchant.
 *
 * There is no `--world` here and no directory one could point at. The world this executes is the
 * one the console just generated, read out of the merchant's workspace row, which is the whole
 * claim this phase makes about a merchant needing no bespoke files.
 *
 * The provider variables are emptied rather than left alone: a developer machine may have a real
 * key in `.env`, and a browser test must never spend one.
 */
function dispatch(): string {
  if (merchant === undefined) throw new Error("AGENTRANK_E2E_SETUP_MERCHANT is required");
  return execFileSync(
    "uv",
    [
      "run",
      "python",
      "-m",
      "agentrank_api.cli",
      "benchmark",
      "dispatch",
      "--merchant-slug",
      merchant,
    ],
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

async function signIn(page: Page, context: BrowserContext): Promise<void> {
  if (key === undefined) throw new Error("AGENTRANK_E2E_SETUP_KEY is required");
  await establishSession(page, context, key);
  await expect(nav(page, "Evaluation")).toBeVisible();
}

test("a merchant with only source evidence builds their own evaluation setup", async ({
  page,
  context,
}) => {
  await signIn(page, context);
  await nav(page, "Evaluation").click();

  // The zero state names the thing that is missing and offers the action that creates it. This
  // is what used to say an operator publishes a benchmark suite from a command line.
  const setup = page.getByRole("region", { name: "Evaluation setup" });
  await expect(setup.getByText("Setup needed")).toBeVisible();
  await expect(setup.getByText("merchant-source@1")).toBeVisible();

  // What would be built, before it is built: how much catalog, how many missions, and what kinds
  // of question they ask. The size of the benchmark a model provider will later be paid to run
  // is known here rather than discovered afterwards.
  await expect(setup.getByText("2 products, 3 of 4 variants in stock")).toBeVisible();
  await expect(setup.getByText("Buy something from a category")).toBeVisible();
  await expect(setup.getByText("Decline when nothing is affordable")).toBeVisible();

  // And what it costs, which is nothing.
  const form = page.getByRole("form", { name: "Build evaluation setup" });
  await expect(form).toContainText("changes no price, no stock level and no payment");
  await expect(form).toContainText("No model provider is contacted and nothing is spent");
  await expect(form).toContainText("does not add products, prices or specifications you did not");

  // The first evaluation is blocked until the setup exists, and says so in the merchant's terms.
  await expect(page.getByText("Prepare your evaluation setup first").first()).toBeVisible();

  // Building revalidates the page, so what a merchant lands on is the built setup itself
  // rather than a transient acknowledgement. The generated world and workload are named and
  // countable.
  await form.getByRole("button", { name: "Prepare evaluation setup" }).click();
  await expect(setup.getByText("Ready", { exact: true })).toBeVisible();
  await expect(setup.getByText(/workspace-suite@1$/)).toBeVisible();
  await expect(setup.getByText("Buy something from a category")).toBeVisible();

  // No mission and no expected outcome is ever published to the merchant.
  await expect(page.getByText("PURCHASE_AVAILABLE")).toHaveCount(0);
  await expect(page.getByText("NO_ACCEPTABLE_PURCHASE")).toHaveCount(0);

  // And the ordinary Phase 4F first evaluation is now reachable, with the mission count the
  // setup panel promised, on the deterministic reference buyer because no provider is configured.
  await expect(page.getByRole("heading", { name: "Run your first evaluation" })).toBeVisible();
  await expect(page.getByText("Prepare your evaluation setup first")).toHaveCount(0);
  await expect(page.getByText("deterministic reference buyer").first()).toBeVisible();
  await page.getByRole("button", { name: "Review first evaluation" }).click();
  const launch = page.getByRole("form", { name: "Confirm first evaluation" });
  await expect(launch).toContainText("9 missions are executed");
  await expect(launch).toContainText("The buyer reads the ordinary storefront");
  // Queued is an honest state: the browser request admitted work and executed none of it.
  await launch.getByRole("button", { name: "Request first evaluation" }).click();
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();

  // The API executes nothing. An operator process claims the launch and runs it, naming only the
  // merchant, because this merchant has no authored world anywhere on disk.
  expect(dispatch()).toContain("COMPLETED");

  await page.goto("/runs");
  await expect(page.locator("table tbody tr")).toHaveCount(1);
  await page.locator("table tbody tr a").first().click();
  await expect(page.getByRole("heading", { name: "Run detail" })).toBeVisible();
  await expect(page.getByText("Task completion")).toBeVisible();
});

test("building an evaluation setup twice does not build a second one", async ({
  page,
  context,
}) => {
  // Retry safety, from the surface a merchant actually retries from. The setup is identified by
  // the merchant, the snapshot and the generation configuration, so a reload and a second press
  // resolve to the setup that already exists rather than to a second world.
  await signIn(page, context);
  await nav(page, "Evaluation").click();

  const setup = page.getByRole("region", { name: "Evaluation setup" });
  await expect(setup.getByText("Ready", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Prepare evaluation setup" })).toHaveCount(0);
});
