import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { record, retainOnFailure, signIn as establishSession } from "./session";

/**
 * The one browser workflow Phase 4C exists for, end to end against real servers.
 *
 * A merchant signs in, reads the evidence behind a proposed fact, accepts one, rejects one,
 * corrects two, is refused a correction the source does not support, and publishes an immutable
 * agent-ready representation. Nothing here is mocked: the console calls the real API, the API
 * writes to a real PostgreSQL, and the seeded run is a real compiler output.
 *
 * The seeded source is built by scripts/seed_compiler_e2e.py so that one run carries both kinds
 * of pending fact: a contradicted wattage that can only be corrected, and an unconfirmed USB-PD
 * claim that can be accepted or rejected.
 */

const key = process.env.AGENTRANK_E2E_KEY;

// Recording is per test and starts once the credential is in, so a failure still leaves the
// trace it always did. See e2e/session.ts.
test.afterEach(async ({ context }, testInfo) => {
  await retainOnFailure(context, testInfo);
});

const WATTAGE_BLACK = "variant.VE-CHG-100-BLK.attribute.wattage";
const WATTAGE_WHITE = "variant.VE-CHG-100-WHT.attribute.wattage";
const COMPATIBILITY_SHORT = "variant.VE-CBL-USBC-1M.compatibility.usb-c-pd";
const COMPATIBILITY_LONG = "variant.VE-CBL-USBC-3M.compatibility.usb-c-pd";

const SOURCE_FIELD = "products[VE-CHG-100].description";

interface CapturedAction {
  url: string;
  headers: Record<string, string>;
  body: string;
}

/** Record the first real compiler write the console sends, so it can be replayed verbatim.

 * Scoped to the compiler pages on purpose: sign in is a server action too, and replaying that
 * one would be replaying a merchant API key rather than a write.
 */
function captureAction(page: Page, captured: CapturedAction[]): void {
  page.on("request", (request) => {
    const headers = request.headers();
    if (request.method() !== "POST" || headers["next-action"] === undefined) return;
    if (!request.url().includes("/compiler/runs/")) return;
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

function reviewOf(page: Page, target: string) {
  return page.getByRole("form", { name: `Review ${target}` });
}

function rowOf(page: Page, target: string) {
  // The variant alone appears in every row for that variant, and the attribute alone appears in
  // rows for other variants, so a fact is only addressable by both.
  const parts = target.split(".");
  return page
    .getByRole("row")
    .filter({ hasText: parts.slice(0, 2).join(".") })
    .filter({ hasText: parts[parts.length - 1] ?? target });
}

async function signIn(page: Page, context: BrowserContext): Promise<void> {
  if (key === undefined) throw new Error("AGENTRANK_E2E_KEY is required");
  await establishSession(page, context, key);
  await page.getByRole("link", { name: "Compiler" }).click();
  await expect(page.getByText("4 semantic fact(s) need review.")).toBeVisible();
  await page.getByRole("link", { name: /voltedge-merchant-source@1/ }).click();
  await expect(page.getByRole("heading", { name: "Review compiler run" })).toBeVisible();
}

async function correct(page: Page, target: string, value: string, excerpt: string): Promise<void> {
  const form = reviewOf(page, target);
  await form.getByLabel("Corrected value").fill(value);
  await form.getByLabel("Source field").fill(SOURCE_FIELD);
  await form.getByLabel("Source excerpt").fill(excerpt);
  await form.getByRole("button", { name: "Confirm correction" }).click();
}

test("a merchant reviews every kind of fact and publishes one immutable representation", async ({
  page,
  context,
}) => {
  const captured: CapturedAction[] = [];
  captureAction(page, captured);
  await signIn(page, context);

  // The proposal a merchant sees is the compiler's reading, not a compiler document.
  await expect(page.getByText("Needs your decision").first()).toBeVisible();

  // Evidence first: the exact source field and excerpt behind the claim.
  const claim = rowOf(page, COMPATIBILITY_SHORT);
  await claim.getByText("Inspect source evidence").click();
  await expect(claim.getByText("products[VE-CBL-USBC].description").first()).toBeVisible();
  await expect(claim).toContainText("USB-PD");

  await reviewOf(page, COMPATIBILITY_SHORT).getByRole("button", { name: "Accept fact" }).click();
  await expect(rowOf(page, COMPATIBILITY_SHORT).getByText("Accepted by you")).toBeVisible();

  await reviewOf(page, COMPATIBILITY_LONG).getByRole("button", { name: "Reject fact" }).click();
  await expect(rowOf(page, COMPATIBILITY_LONG).getByText("Rejected by you")).toBeVisible();

  // A correction the cited source does not support is refused, in place, with the entry kept.
  await correct(page, WATTAGE_BLACK, "999", "65W");
  const refused = reviewOf(page, WATTAGE_BLACK);
  await expect(refused.getByRole("alert")).toContainText("not supported by cited source evidence");
  await expect(refused.getByLabel("Corrected value")).toHaveValue("999");
  await expect(refused.getByLabel("Source excerpt")).toHaveValue("65W");

  await correct(page, WATTAGE_BLACK, "65", "65W");
  await expect(rowOf(page, WATTAGE_BLACK).getByText("Corrected by you")).toBeVisible();
  await expect(rowOf(page, WATTAGE_BLACK).getByText("Corrected to 65 W")).toBeVisible();

  // Publication is blocked while anything is still open, and says so from the backend.
  await expect(page.getByText("1 fact(s) still require review.")).toBeVisible();

  await correct(page, WATTAGE_WHITE, "65", "65W");
  await expect(rowOf(page, WATTAGE_WHITE).getByText("Corrected by you")).toBeVisible();

  // The review is separate evidence: the compiler proposal is still shown beside the decision.
  await rowOf(page, WATTAGE_BLACK).getByText("Inspect source evidence").click();
  await expect(
    rowOf(page, WATTAGE_BLACK).getByText("CORRECT recorded by MERCHANT_CREDENTIAL"),
  ).toBeVisible();
  await expect(page.getByText("which is unchanged").first()).toBeVisible();

  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByText("does not rerun a benchmark")).toBeVisible();
  await page.getByRole("button", { name: "Publish representation" }).click();

  const publication = page.getByRole("region", { name: "Publication" });
  await expect(publication).toContainText("Agent-ready representation published:");
  const identity = ((await publication.innerText()).match(/[0-9a-f]{8}-[0-9a-f-]{27}/) ?? [])[0];
  expect(identity).toBeDefined();

  // Nothing is left to decide, and no form offers to change a published run.
  await expect(page.getByRole("button", { name: "Accept fact" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Confirm correction" })).toHaveCount(0);
  await expect(page.getByText("can no longer change")).toBeVisible();

  // Publishing again is refused rather than producing a second representation.
  await page.reload();
  await expect(page.getByRole("button", { name: "Review publication" })).toHaveCount(0);

  await page.getByRole("link", { name: "Compiler" }).click();
  await expect(page.getByText("All required reviews are resolved.")).toBeVisible();
  await expect(page.getByText(`Published representation: ${String(identity)}`)).toBeVisible();

  // The same signed in browser, the same recorded compiler write, and only the origin changed.
  // One is executed and answered; the other never reaches the action at all.
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
});

test("the console shows nothing about a compiler run without a signed in session", async ({
  page,
  context,
}) => {
  // Nothing here holds a credential, so this one records from the top.
  await record(context);
  await context.clearCookies();
  await page.goto("/compiler");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByLabel("Merchant API key")).toBeVisible();
});
