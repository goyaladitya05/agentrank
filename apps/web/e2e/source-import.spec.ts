import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { retainOnFailure, signIn as establishSession } from "./session";

/**
 * The Phase 5E import workflow, end to end against real servers, a real database and real sockets.
 *
 * A merchant who has published nothing at all signs in, names four of their own public pages and
 * one policy page, and AgentRank fetches them over an ordinary HTTP connection, reads what they
 * publish, and shows the result. The merchant reads it, sees which page could not be imported and
 * why, states the one number no public page publishes, and creates their first source snapshot.
 * From there the ordinary Phase 5C bootstrap builds their evaluation setup and the ordinary Phase
 * 4F preflight is reachable.
 *
 * Nothing is mocked. `scripts/serve_import_fixture.py` is an ordinary web server on loopback with
 * five invented pages on it, started by `playwright.config.ts` beside the API and the console. The
 * merchant has no source document, no world directory, no catalog fixture and no hand written row:
 * `scripts/seed_import_e2e.py` creates a merchant and a credential and stops there.
 *
 * The run stops before any live model execution, and no model provider is contacted at any point.
 * The import is deterministic and the bootstrap is deterministic; requesting an evaluation stays
 * its own explicit command and this workflow does not press it.
 */

const key = process.env.AGENTRANK_E2E_IMPORT_KEY;
const storefront = process.env.AGENTRANK_E2E_IMPORT_STOREFRONT ?? "http://127.0.0.1:8002";

test.afterEach(async ({ context }, testInfo) => {
  await retainOnFailure(context, testInfo);
});

/** The console navigation, so a link in it is never confused with one in the page body. */
function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Console" }).getByRole("link", { name });
}

async function signIn(page: Page, context: BrowserContext): Promise<void> {
  if (key === undefined) throw new Error("AGENTRANK_E2E_IMPORT_KEY is required");
  await establishSession(page, context, key);
  await expect(nav(page, "Source")).toBeVisible();
}

test("a merchant imports their own public pages into their first source snapshot", async ({
  page,
  context,
}) => {
  await signIn(page, context);
  await nav(page, "Source").click();

  // A merchant with nothing is offered the import rather than only a JSON editor.
  await expect(page.getByText("You have no source snapshot yet.")).toBeVisible();
  await page.getByRole("link", { name: "Import it from your own public pages" }).click();
  await expect(page.getByRole("heading", { name: "Import your pages" })).toBeVisible();

  // What the merchant supplies is which of their pages to read, and nothing about how.
  const form = page.getByRole("form", { name: "Import public pages from your store" });
  await expect(form).toContainText("Public pages only");
  await expect(form).toContainText("signs in to nothing");
  await expect(form).toContainText("submits no form on your site");

  await form.getByLabel("A product page URL").fill(`${storefront}/p/charger`);
  await form
    .getByLabel("More product page URLs, one per line")
    .fill([`${storefront}/p/sleeve`, `${storefront}/p/dock`, `${storefront}/p/lamp`].join("\n"));
  await form.getByLabel("Returns policy page URL").fill(`${storefront}/returns`);
  await form.getByRole("button", { name: "Read these pages" }).click();

  // Reading the pages produced a record and no source history.
  await expect(page.getByText("Nothing has become your source yet")).toBeVisible();
  await page.getByRole("link", { name: "Review what AgentRank read" }).click();
  await expect(page.getByRole("heading", { name: "Imported pages" })).toBeVisible();

  // What was read, counted rather than graded.
  const review = page.getByRole("region", { name: "What AgentRank read" });
  await expect(review).toContainText("5 of 5 answered");
  await expect(review).toContainText("3 product(s), 4 variant(s), 1 policy text(s)");
  await expect(review).toContainText("Not created. Nothing here is your source yet.");

  // Every page it fetched, with the digest of what arrived.
  // Labelled on the scroll container the table sits in, which is where this console puts the
  // accessible name so that the region is reachable by keyboard as well as named.
  const pages = page.getByLabel("Pages read");
  await expect(pages).toContainText(`${storefront}/p/charger`);
  await expect(pages).toContainText(`${storefront}/returns`);
  await expect(pages.getByText("HTTP 200").first()).toBeVisible();

  // Every product names the page and the method behind it, and availability is never a quantity.
  const products = page.getByLabel("Products extracted");
  await expect(products).toContainText("65W Travel Charger");
  await expect(products).toContainText("Laptop Sleeve");
  await expect(products).toContainText("Structured product data");
  await expect(products).toContainText("INR 3299.00");
  await expect(products).toContainText("In stock, no quantity published");
  await expect(products).toContainText("Out of stock");

  // The one page that could not be read honestly is named with the reason, and is not a product.
  await expect(review).toContainText("currency_missing");
  await expect(review).toContainText(`${storefront}/p/lamp`);
  await expect(products).not.toContainText("Desk Lamp");

  // The merchant's own policy prose is evidence and is shown as evidence.
  await expect(review).toContainText("Returns are accepted within 30 days");
  // A script on the merchant's page is not merchant evidence and never became text.
  await expect(review).not.toContainText("never merchant evidence");

  // Availability is read from the pages and no number is asked of anybody. The form used to ask
  // for a stock level, because a source variant needed an exact count and no public page
  // publishes one; that was the last place this workflow put a figure nobody had published into
  // a merchant's own immutable history, and it is gone.
  const confirm = page.getByRole("form", { name: "Create a source snapshot from this import" });
  await expect(confirm).toContainText("Nothing is compiled and no evaluation runs");
  await expect(confirm.getByLabel("Evaluation stock level")).toHaveCount(0);
  await confirm.getByRole("button", { name: "Create source snapshot" }).click();

  await expect(page.getByText("Source snapshot merchant-source@1 created")).toBeVisible();
  await expect(page.getByText("no evaluation has run")).toBeVisible();

  // The snapshot is an ordinary one, and says where it came from.
  await page.getByRole("link", { name: "Open this source snapshot" }).click();
  await expect(page.getByRole("heading", { name: "Source snapshot" }).first()).toBeVisible();
  await expect(page.getByText("Imported from your own pages")).toBeVisible();
  await expect(page.getByText("import_availability").first()).toBeVisible();

  // And the ordinary Phase 5C bootstrap builds an evaluation setup from it, with no compiler run,
  // no published representation and no operator anywhere in the workflow.
  await nav(page, "Evaluation").click();
  const setup = page.getByRole("region", { name: "Evaluation setup" });
  await expect(setup.getByText("Setup needed")).toBeVisible();
  await expect(setup.getByText("merchant-source@1")).toBeVisible();
  await page
    .getByRole("form", { name: "Build evaluation setup" })
    .getByRole("button", { name: "Prepare evaluation setup" })
    .click();
  await expect(setup.getByText("Ready", { exact: true })).toBeVisible();

  // The first evaluation is reachable and is not requested here. This workflow stops before any
  // model execution, and the console says which buyer would run it.
  await expect(page.getByRole("heading", { name: "Run your first evaluation" })).toBeVisible();
  await expect(page.getByText("deterministic reference buyer").first()).toBeVisible();
});

test("a merchant is refused a target AgentRank will not fetch", async ({ page, context }) => {
  // The security boundary from the surface a merchant reaches it through. The console sends the
  // URL and the API decides; nothing about which addresses are permitted is duplicated in the
  // browser, so this is the real refusal rather than a client side one.
  await signIn(page, context);
  await page.goto("/sources/import");

  const form = page.getByRole("form", { name: "Import public pages from your store" });
  await form.getByLabel("A product page URL").fill("http://169.254.169.254/latest/meta-data/");
  await form.getByRole("button", { name: "Read these pages" }).click();

  // Scoped to the form, because the framework's own route announcer is also an alert.
  const alert = form.getByRole("alert");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("public internet address");
  // The form kept what was typed, and nothing was recorded as an import.
  await expect(form.getByLabel("A product page URL")).toHaveValue(
    "http://169.254.169.254/latest/meta-data/",
  );
});
