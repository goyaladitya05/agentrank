import { execFileSync } from "node:child_process";

import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { retainOnFailure, signIn as establishSession } from "./session";

/**
 * The Buildathon walk: the whole merchant story, screen after screen, against real servers.
 *
 * A merchant signs in, runs their first evaluation, reads which shopping scenarios failed and
 * why, opens the one that needs their attention, reviews the facts AgentRank proposes, publishes
 * them, measures again, and reads the result beside the run before it. A judge who watches this
 * sequence has seen the product; nothing in it requires a benchmark identifier, a compiler term
 * or a UUID.
 *
 * Deterministic reference execution throughout: the dispatcher runs with the provider variables
 * emptied, no model provider is contacted, and the comparison at the end is whatever the engine
 * honestly concludes about two reference runs, which this spec asserts rather than dressing up.
 *
 * The seeded merchant comes from scripts/seed_buildathon_e2e.py: a three mission suite whose
 * specification mission fails because the world omits the wattage it requires, and a source
 * document carrying the compiler seed's two ambiguities so the fixes page has real decisions.
 */

const key = process.env.AGENTRANK_E2E_BUILDATHON_KEY;
const world = process.env.AGENTRANK_E2E_BUILDATHON_WORLD;

// Two real benchmark runs of three missions each, plus every merchant page between them.
test.setTimeout(300_000);

test.afterEach(async ({ context }, testInfo) => {
  await retainOnFailure(context, testInfo);
});

/** Run the operator dispatcher once, with no provider reachable. */
function dispatch(): string {
  if (world === undefined) throw new Error("AGENTRANK_E2E_BUILDATHON_WORLD is required");
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

function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Console" }).getByRole("link", { name });
}

function fixOf(page: Page, target: string) {
  return page.getByRole("article", { name: `Fix ${target}` });
}

function reviewOf(page: Page, target: string) {
  return page.getByRole("form", { name: `Review ${target}` });
}

async function correctWattage(page: Page, target: string): Promise<void> {
  const form = reviewOf(page, target);
  await form.getByLabel("Corrected value").fill("65");
  await form.getByLabel("Source field").fill("products[VE-CHG-100].description");
  await form.getByLabel("Source excerpt").fill("65W");
  await form.getByRole("button", { name: "Confirm correction" }).click();
  await expect(fixOf(page, target).getByText("Corrected by you")).toBeVisible();
}

async function signIn(page: Page, context: BrowserContext): Promise<void> {
  if (key === undefined) throw new Error("AGENTRANK_E2E_BUILDATHON_KEY is required");
  await establishSession(page, context, key);
  await expect(nav(page, "Overview")).toBeVisible();
}

test("a judge's walk: evaluate, read the issues, review the fixes, publish, measure again", async ({
  page,
  context,
}) => {
  await signIn(page, context);

  // 1. The overview asks the merchant's own question and leads to the first evaluation.
  await page.goto("/overview");
  await expect(page.getByText("Can AI shopping agents buy from your store?")).toBeVisible();
  await page.getByRole("link", { name: "Run your first evaluation" }).click();

  // 2. The preflight speaks in scenarios and models, not in benchmark identities.
  await expect(page.getByRole("heading", { name: "Run your first evaluation" })).toBeVisible();
  await expect(page.getByText("3, one at a time")).toBeVisible();
  await page.getByRole("button", { name: "Run evaluation" }).click();
  const confirm = page.getByRole("form", { name: "Confirm first evaluation" });
  await expect(confirm).toContainText("3 shopping scenarios are executed");
  await confirm.getByRole("button", { name: "Request first evaluation" }).click();
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();

  // The API executes nothing; the operator process does.
  expect(dispatch()).toContain("COMPLETED");

  // 3. The overview now leads with results a judge can read in seconds.
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: /purchase scenarios completed/ })).toBeVisible();
  await expect(page.getByText("1 scenario needs your attention")).toBeVisible();
  await expect(page.getByRole("region", { name: "What needs attention" })).toContainText(
    "identified nothing to buy",
  );

  // 4. Issues separate what needs the merchant from what does not.
  await nav(page, "Issues").click();
  const attention = page.getByRole("region", { name: "Needs your attention" });
  await expect(attention).toContainText("identified nothing to buy");
  const external = page.getByRole("region", { name: "No action required" });
  await expect(external).toContainText("No action required from you");

  // 5. One issue, opened: what happened, who owns it, and the evidence trail.
  await attention.getByRole("link", { name: "Open this issue" }).click();
  await expect(page.getByRole("heading", { name: /identified nothing to buy/ })).toBeVisible();
  await expect(page.getByText("Who owns this")).toBeVisible();
  await expect(page.getByRole("link", { name: "View evidence" }).first()).toBeVisible();

  // 6. The fixes: current information beside the proposed agent-ready fact, evidence attached.
  await nav(page, "Fixes").click();
  await expect(page.getByText("AgentRank found 4 facts you can review")).toBeVisible();
  await page.getByRole("link", { name: "Review 4 fixes" }).click();
  await expect(page.getByRole("heading", { name: "Review fixes" })).toBeVisible();
  const wattage = fixOf(page, "variant.VE-CHG-100-BLK.attribute.wattage");
  await expect(wattage.getByText("Proposed agent-ready fact")).toBeVisible();
  await wattage.getByText("View evidence").click();
  await expect(wattage.getByText("100W").first()).toBeVisible();

  await correctWattage(page, "variant.VE-CHG-100-BLK.attribute.wattage");
  await correctWattage(page, "variant.VE-CHG-100-WHT.attribute.wattage");
  await reviewOf(page, "variant.VE-CBL-USBC-1M.compatibility.usb-c-pd")
    .getByRole("button", { name: "Accept fact" })
    .click();
  await expect(
    fixOf(page, "variant.VE-CBL-USBC-1M.compatibility.usb-c-pd").getByText("Accepted by you"),
  ).toBeVisible();
  await reviewOf(page, "variant.VE-CBL-USBC-3M.compatibility.usb-c-pd")
    .getByRole("button", { name: "Reject fact" })
    .click();
  await expect(
    fixOf(page, "variant.VE-CBL-USBC-3M.compatibility.usb-c-pd").getByText("Rejected by you"),
  ).toBeVisible();

  // 7. Publish, through an explicit confirmation that says what publishing does not do.
  await page.getByRole("button", { name: "Publish fixes" }).click();
  await expect(page.getByText("does not rerun a benchmark")).toBeVisible();
  await page.getByRole("button", { name: "Publish fixes" }).click();
  const published = page.getByRole("region", { name: "Publish" });
  await expect(published).toContainText("These fixes are published.");

  // 8. Measure again is the natural next step, offered where the publish landed.
  await published.getByRole("link", { name: "Measure again" }).click();
  await expect(page.getByRole("heading", { name: "Measure again" })).toBeVisible();
  await page.getByRole("button", { name: "Measure again" }).click();
  const reconfirm = page.getByRole("form", { name: "Confirm re-evaluation" });
  await expect(reconfirm).toContainText("3 shopping scenarios are executed");
  await reconfirm.getByRole("button", { name: "Request re-evaluation" }).click();
  await expect(page.getByText("Queued", { exact: true })).toBeVisible();

  expect(dispatch()).toContain("COMPLETED");

  // 9. The comparison: before beside after, caveats attached, nothing dressed up. Two
  // deterministic reference runs of the same world honestly conclude parity, and the page
  // says that rather than manufacturing an improvement.
  await page.locator('[aria-label="Evaluations"] a').first().click();
  await expect(page.getByText("3 of 3 scenarios finished")).toBeVisible();
  await expect(page.getByText("purchase scenarios completed").first()).toBeVisible();
  await expect(page.getByText("Before", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("After", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Not a controlled experiment", { exact: true })).toBeVisible();
  await expect(
    page.getByText("No scenario changed its outcome between the two runs."),
  ).toBeVisible();

  // The whole walk happened on merchant surfaces. The Lab still holds the technical view.
  await page.goto("/lab/runs");
  await expect(page.locator("table tbody tr")).toHaveCount(2);
});
