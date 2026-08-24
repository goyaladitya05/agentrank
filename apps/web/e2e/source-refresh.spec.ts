import { expect, test, type Page } from "@playwright/test";

import { record, retainOnFailure, signIn as establishSession } from "./session";

/**
 * The one browser workflow Phase 4E exists for, end to end against real servers.
 *
 * The merchant this runs against is finished: they compiled their source, had nothing left to
 * review, and published an immutable representation. That is the dead end the phase is about.
 * From there the only way forward is historical, and this drives the whole of it: supply newer
 * source evidence, get a new immutable snapshot, run the deterministic compiler over it, land in
 * the review workflow Phase 4C already built, answer the fact the newer evidence made ambiguous,
 * and publish a second representation beside the first.
 *
 * Nothing here is mocked. The console calls the real API, the API writes to a real PostgreSQL,
 * and the compiler run is a real compiler output over a document this test typed.
 *
 * What it also proves is what did not happen. The first representation is still exactly what it
 * was, publishing the second one was an explicit act with its own confirmation, and measuring it
 * again is still a separate command nobody here ran.
 */

const key = process.env.AGENTRANK_E2E_KEY_SOURCE;
const publishedBefore = process.env.AGENTRANK_E2E_SOURCE_REPRESENTATION;

test.afterEach(async ({ context }, testInfo) => {
  await retainOnFailure(context, testInfo);
});

const WATTAGE_BLACK = "variant.VE-CHG-100-BLK.attribute.wattage";
const WATTAGE_WHITE = "variant.VE-CHG-100-WHT.attribute.wattage";
const SOURCE_FIELD = "products[VE-CHG-100].description";
const CONTRADICTION = "Explicitly supports 65W, unlike its 100W title.";

/** The console navigation, so a link named "Source" is the nav one and not a row link. */
function nav(page: Page) {
  return page.getByRole("navigation", { name: "Console" });
}

function reviewOf(page: Page, target: string) {
  return page.getByRole("form", { name: `Review ${target}` });
}

function rowOf(page: Page, target: string) {
  const parts = target.split(".");
  return page
    .getByRole("row")
    .filter({ hasText: parts.slice(0, 2).join(".") })
    .filter({ hasText: parts[parts.length - 1] ?? target });
}

async function correct(page: Page, target: string): Promise<void> {
  const form = reviewOf(page, target);
  await form.getByLabel("Corrected value").fill("65");
  await form.getByLabel("Source field").fill(SOURCE_FIELD);
  await form.getByLabel("Source excerpt").fill("65W");
  await form.getByRole("button", { name: "Confirm correction" }).click();
}

test("a merchant supplies newer source evidence, compiles it and publishes a second representation", async ({
  page,
  context,
}) => {
  if (key === undefined) throw new Error("AGENTRANK_E2E_KEY_SOURCE is required");
  if (publishedBefore === undefined) {
    throw new Error("AGENTRANK_E2E_SOURCE_REPRESENTATION is required");
  }
  await establishSession(page, context, key);

  // The dead end: everything is reviewed and one representation is published.
  await nav(page).getByRole("link", { name: "Compiler" }).click();
  await expect(page.getByText("All required reviews are resolved.")).toBeVisible();
  await expect(page.getByText(`Published representation: ${publishedBefore}`)).toBeVisible();

  // The source history the merchant starts from.
  await nav(page).getByRole("link", { name: "Source" }).click();
  await expect(page.getByRole("heading", { name: "Source", exact: true })).toBeVisible();
  await expect(page.getByText("voltedge-merchant-source@1").first()).toBeVisible();
  await expect(page.getByText("Published by an operator").first()).toBeVisible();

  // Newer evidence, typed into the merchant's own current document.
  await page.getByRole("link", { name: "Supply newer source evidence" }).first().click();
  const editor = page.getByRole("textbox", { name: "Source document" });
  const current: unknown = JSON.parse(await editor.inputValue());
  expect(current).toHaveProperty("products");
  const document = current as { products: { description: string; external_id?: string }[] };
  const first = document.products[0];
  expect(first).toBeDefined();
  if (first === undefined) return;
  first.description = CONTRADICTION;

  // A document that is not a document is refused before it is sent, in place.
  await editor.fill("{ not json");
  await page.getByRole("button", { name: "Submit source document" }).click();
  await expect(page.locator("#source-document-error")).toContainText("not valid JSON");
  await expect(editor).toHaveValue("{ not json");

  // And one the API refuses is refused the same way, with the field named and the document
  // still in the editor. This is the round trip: the message can only have come from the API.
  const rejected = JSON.parse(JSON.stringify(document)) as typeof document;
  (rejected.products[0] as { external_id: string }).external_id = "VE.CHG.100";
  await editor.fill(JSON.stringify(rejected, null, 2));
  await page.getByRole("button", { name: "Submit source document" }).click();
  await expect(page.locator("#source-document-error")).toContainText("external_id");
  await expect(editor).toHaveValue(JSON.stringify(rejected, null, 2));

  await editor.fill(JSON.stringify(document, null, 2));
  await page.getByRole("button", { name: "Submit source document" }).click();
  await expect(page.getByText("Source snapshot created.")).toBeVisible();
  await expect(page.getByText("Nothing has been compiled yet.")).toBeVisible();

  // The new snapshot is its own immutable artifact, and the evidence it carries is addressable.
  await page.getByRole("link", { name: "Open this source snapshot" }).click();
  await expect(
    page.getByRole("heading", { name: "Source snapshot voltedge-merchant-source@2" }),
  ).toBeVisible();
  await expect(page.getByText("This snapshot never changes.")).toBeVisible();
  const evidence = page.getByRole("region", { name: "Source evidence" });
  await expect(evidence.getByText(SOURCE_FIELD)).toBeVisible();
  await expect(evidence.getByText(CONTRADICTION)).toBeVisible();

  // Compiling is its own command, behind its own statement of what it does not do.
  const compile = page.getByRole("form", { name: "Run the compiler" });
  await expect(compile).toContainText("This publishes nothing and starts no benchmark.");
  await expect(compile).toContainText("No price, stock level or order changes.");
  await compile.getByRole("button", { name: "Run the compiler" }).click();
  // The acknowledgement counts what is actually unanswered rather than asserting that something
  // is. A run that could not read its snapshot is answered the same way and says so instead.
  await expect(page.getByText("Compiler run finished. 2 facts need your decision.")).toBeVisible();

  // And it ends in the review workflow that already existed.
  await page.getByRole("link", { name: "Review this compiler run" }).click();
  await expect(page.getByRole("heading", { name: "Review compiler run" })).toBeVisible();
  await expect(page.getByText("voltedge-merchant-source@2")).toBeVisible();
  await expect(page.getByText("2 fact(s) still require review.")).toBeVisible();

  // The fact the newer evidence made ambiguous cites a field of the product it is about.
  const wattage = rowOf(page, WATTAGE_BLACK);
  await wattage.getByText("Inspect source evidence").click();
  await expect(wattage.getByText("products[VE-CHG-100].").first()).toBeVisible();

  // And the correction proves which snapshot the run actually read. The API refuses a
  // measurement whose value is not in the text at the field it cites, and "65W" appears only in
  // the description this test typed. A run over the previous snapshot would refuse this.
  await correct(page, WATTAGE_BLACK);
  await expect(rowOf(page, WATTAGE_BLACK).getByText("Corrected by you")).toBeVisible();
  await correct(page, WATTAGE_WHITE);
  await expect(rowOf(page, WATTAGE_WHITE).getByText("Corrected by you")).toBeVisible();

  // Publication stays explicit and still says what it does not do.
  await page.getByRole("button", { name: "Review publication" }).click();
  await expect(page.getByText("does not rerun a benchmark")).toBeVisible();
  await page.getByRole("button", { name: "Publish representation" }).click();

  const publication = page.getByRole("region", { name: "Publication" });
  await expect(publication).toContainText("Agent-ready representation published:");
  const identity = ((await publication.innerText()).match(/[0-9a-f]{8}-[0-9a-f-]{27}/) ?? [])[0];
  expect(identity).toBeDefined();
  expect(identity).not.toBe(publishedBefore);

  // Measuring it again is still a command nobody here ran, and the launch history proves it
  // rather than the sentence beside the link.
  await expect(publication).toContainText("request a re-evaluation");
  await nav(page).getByRole("link", { name: "Re-evaluation" }).click();
  await expect(page.getByText("No re-evaluations yet")).toBeVisible();

  // The first representation and the run behind it are exactly what they were.
  await nav(page).getByRole("link", { name: "Source" }).click();
  await expect(
    page.getByRole("row").filter({ hasText: "voltedge-merchant-source@1" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "voltedge-merchant-source@1" }).click();
  await expect(page.getByText("Superseded by newer evidence")).toBeVisible();
  await expect(
    page.getByText("This snapshot has already been read by the compiler."),
  ).toBeVisible();
  await page.getByRole("link", { name: "Review the compiler run for this snapshot" }).click();
  await expect(
    page.getByRole("region", { name: "Publication" }).getByText(publishedBefore),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Review publication" })).toHaveCount(0);
});

test("the console shows nothing about a source snapshot without a signed in session", async ({
  page,
  context,
}) => {
  // Nothing here holds a credential, so this one records from the top.
  await record(context);
  await context.clearCookies();
  await page.goto("/sources");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByLabel("Merchant API key")).toBeVisible();
});
