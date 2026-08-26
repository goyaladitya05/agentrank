import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Confirmed, ImportReview } from "./ImportReview";
import { ImportRead, RunImportForm } from "./RunImport";
import type { ImportProduct, SourceImport } from "@/lib/import";
import {
  IDLE_CONFIRM,
  IDLE_IMPORT,
  type ConfirmState,
  type ImportState,
} from "@/lib/import-mutation";

/**
 * What a merchant reads about their own store, asserted as sentences rather than as structure.
 *
 * Two things these tests exist for beyond the ordinary rendering checks.
 *
 * The first is that an imported "In stock" never appears as a quantity anywhere on the page. That
 * is the property the whole extraction boundary is built around, and it would be undone by one
 * helpful column.
 *
 * The second is that merchant page content is rendered as text. Every string here came from
 * somebody else's web page, and a test that a script tag arrives escaped is a test that this page
 * cannot be turned into a delivery mechanism by a merchant AgentRank fetched.
 */

const CHARGER: ImportProduct = {
  external_id: "product-VE-65",
  title: "VoltEdge 65W GaN Charger",
  description: "A compact charger.",
  category: "Chargers",
  source_url: "https://shop.example/p/charger",
  extraction: "STRUCTURED_DATA",
  variants: [
    {
      sku: "VE-65-BLK",
      label: "Black",
      price_amount_minor: 349900,
      currency: "INR",
      availability: "IN_STOCK",
      availability_text: "https://schema.org/InStock",
    },
    {
      sku: "VE-65-WHT",
      label: "White",
      price_amount_minor: 349900,
      currency: "INR",
      availability: "OUT_OF_STOCK",
      availability_text: "https://schema.org/OutOfStock",
    },
  ],
};

const IMPORT: SourceImport = {
  summary: {
    import_id: "11111111-1111-4111-8111-111111111111",
    origin: "https://shop.example:443",
    state: "COMPLETED",
    failure_reason: null,
    created_at: "2026-08-26T10:00:00Z",
    page_count: 3,
    retrieved_count: 2,
    product_count: 1,
    variant_count: 2,
    policy_count: 1,
    omission_count: 1,
    source_snapshot_id: null,
    confirmed_at: null,
  },
  pages: [
    {
      url: "https://shop.example/p/charger",
      kind: "PRODUCT",
      name: null,
      retrieved: true,
      reason: null,
      detail: null,
      status_code: 200,
      byte_count: 4096,
      content_hash: "sha256:abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
      final_url: "https://shop.example/p/charger",
      redirect_count: 0,
      retrieved_at: "2026-08-26T10:00:00Z",
    },
    {
      url: "https://shop.example/returns",
      kind: "POLICY",
      name: "returns",
      retrieved: true,
      reason: null,
      detail: null,
      status_code: 200,
      byte_count: 1024,
      content_hash: "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      final_url: "https://shop.example/returns",
      redirect_count: 1,
      retrieved_at: "2026-08-26T10:00:00Z",
    },
    {
      url: "https://shop.example/p/gone",
      kind: "PRODUCT",
      name: null,
      retrieved: false,
      reason: "http_error",
      detail: "that page answered with an error status",
      status_code: 404,
      byte_count: 0,
      content_hash: null,
      final_url: null,
      redirect_count: 0,
      retrieved_at: "2026-08-26T10:00:00Z",
    },
  ],
  products: [CHARGER],
  policies: [
    {
      name: "returns",
      body: "Return any unopened item within 30 days.",
      source_url: "https://shop.example/returns",
      truncated: false,
    },
  ],
  omissions: [
    {
      source_url: "https://shop.example/p/mystery",
      code: "currency_missing",
      detail: "the page publishes no currency for this price",
      subject: null,
    },
  ],
  findings: [],
  blockers: [],
  stock_level_required: true,
  stock_level: null,
  confirmable: true,
  max_stock_level: 10000,
};

function review(overrides: Partial<SourceImport> = {}): string {
  return renderToStaticMarkup(
    <ImportReview found={{ ...IMPORT, ...overrides }} action={() => IDLE_CONFIRM} />,
  );
}

function confirmed(state: Partial<ConfirmState>): string {
  return renderToStaticMarkup(<Confirmed state={{ ...IDLE_CONFIRM, ok: true, ...state }} />);
}

function importForm(state: ImportState = IDLE_IMPORT, pending = false): string {
  return renderToStaticMarkup(
    <RunImportForm action="/sources/import" state={state} pending={pending} />,
  );
}

describe("<ImportReview>", () => {
  it("counts what happened rather than grading it", () => {
    const html = review();
    expect(html).toContain("2 of 3 answered");
    expect(html).toContain("1 product(s), 2 variant(s), 1 policy text(s)");
    expect(html).toContain("1 item(s), listed below");
  });

  it("says nothing is the merchant's source until they confirm it", () => {
    expect(review()).toContain("Not created. Nothing here is your source yet.");
  });

  it("names the page and the method behind every product", () => {
    const html = review();
    expect(html).toContain("Structured product data");
    expect(html).toContain("https://shop.example/p/charger");
  });

  it("never turns an in stock page into a quantity", () => {
    const html = review();
    expect(html).toContain("In stock, no quantity published");
    expect(html).toContain("Out of stock");
    // No digit adjacent to an availability claim anywhere, and in particular none of the numbers
    // an importer that invented one would reach for.
    expect(html).not.toContain("999");
    expect(html).not.toMatch(/In stock[^<]*\d/);
  });

  it("asks for the stock level as the merchant's own statement", () => {
    const html = review();
    expect(html).toContain("These pages say what is available and not how much of it");
    expect(html).toContain("Your own words about your catalog, not a figure read from your store");
    expect(html).toContain('name="stock_level"');
  });

  it("does not ask for a stock level when every page said out of stock", () => {
    const html = review({ stock_level_required: false });
    expect(html).not.toContain('name="stock_level"');
  });

  it("lists what was not imported with the reason and the page", () => {
    const html = review();
    expect(html).toContain("currency_missing");
    expect(html).toContain("the page publishes no currency for this price");
    expect(html).toContain("https://shop.example/p/mystery");
  });

  it("reports a page that did not answer rather than hiding it", () => {
    const html = review();
    expect(html).toContain("Not read");
    expect(html).toContain("that page answered with an error status");
    expect(html).toContain("Redirected 1 time(s)");
  });

  it("says what confirming does and does not do", () => {
    const html = review().toLowerCase();
    expect(html).toContain("your existing snapshots never change");
    expect(html).toContain("nothing is compiled and no evaluation runs");
    expect(html).toContain("does not change any price, stock level or order on your store");
  });

  it("blocks confirmation with a stated reason when there is nothing to confirm", () => {
    const html = review({
      products: [],
      confirmable: false,
      blockers: [{ code: "no_products", detail: "no product could be imported from these pages" }],
    });
    expect(html).toContain("No product could be extracted from these pages.");
    expect(html).toContain("no_products");
    expect(html).toContain("disabled");
  });

  it("renders merchant page content as text and never as markup", () => {
    const html = review({
      products: [
        {
          ...CHARGER,
          title: '<img src=x onerror="alert(1)">',
          description: "<script>window.stolen = 1</script>",
        },
      ],
      policies: [
        {
          name: "returns",
          body: "<script>alert(2)</script>",
          source_url: "https://shop.example/returns",
          truncated: false,
        },
      ],
    });
    // The markup React itself emits contains a script tag, so the assertion is about the
    // merchant's payloads specifically: each one appears escaped and none of them appears live.
    expect(html).toContain("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
    expect(html).toContain("&lt;script&gt;alert(2)&lt;/script&gt;");
    expect(html).not.toContain("<script>alert");
    expect(html).not.toContain("<img src=x");
    expect(html).not.toContain('onerror="alert');
  });

  it("shows a merchant URL as text rather than as a link to follow", () => {
    const html = review();
    expect(html).not.toContain('href="https://shop.example');
  });

  it("points at the snapshot once this import has been confirmed", () => {
    const html = review({
      summary: { ...IMPORT.summary, source_snapshot_id: "22222222-2222-4222-8222-222222222222" },
    });
    expect(html).toContain("already been confirmed");
    expect(html).toContain("/sources/22222222-2222-4222-8222-222222222222");
  });

  it("uses no marketing language anywhere", () => {
    const html = review().toLowerCase();
    for (const banned of ["optimi", "unlock", "ai understood", "discovered", "opportunit"]) {
      expect(html).not.toContain(banned);
    }
  });
});

describe("<Confirmed>", () => {
  it("distinguishes a new snapshot from one that already said the same thing", () => {
    expect(
      confirmed({ createdSnapshot: true, sourceLabel: "merchant-source@2", snapshotId: "abc" }),
    ).toContain("Source snapshot merchant-source@2 created");
    expect(confirmed({ createdSnapshot: false, snapshotId: "abc" })).toContain(
      "says the same thing as your current source snapshot",
    );
  });

  it("offers the next step without taking it", () => {
    const html = confirmed({ createdSnapshot: true, snapshotId: "abc" });
    expect(html).toContain("Continue to your evaluation setup");
    expect(html).toContain("no evaluation has run");
  });
});

describe("<RunImportForm>", () => {
  it("labels every URL field and offers no crawler settings", () => {
    const html = importForm();
    expect(html).toContain("A product page URL");
    expect(html).toContain("Returns policy page URL");
    expect(html).toContain("Warranty page URL");
    expect(html).toContain("Shipping page URL");
    for (const banned of ["depth", "user agent", "concurrency", "selector", "crawl"]) {
      expect(html.toLowerCase()).not.toContain(banned);
    }
  });

  it("states what AgentRank will and will not do to the merchant's site", () => {
    const html = importForm().toLowerCase();
    expect(html).toContain("public pages only");
    expect(html).toContain("signs in to nothing");
    expect(html).toContain("submits no form on your site");
    expect(html).toContain("at most twelve");
  });

  it("keeps what was typed when the import is refused, and associates the error", () => {
    const values = {
      storefront: "https://shop.example/p/one",
      products: "https://shop.example/p/two",
      returns: "",
      warranty: "",
      shipping: "",
    };
    const html = importForm({
      ...IDLE_IMPORT,
      message: "Enter at least one public page URL from your store.",
      values,
    });
    expect(html).toContain("https://shop.example/p/one");
    expect(html).toContain("https://shop.example/p/two");
    expect(html).toContain('role="alert"');
    expect(html).toContain('id="import-error"');
    expect(html).toContain('aria-describedby="import-error"');
  });

  it("announces that a fetch is in flight and does not point at an absent error", () => {
    const html = importForm({ ...IDLE_IMPORT, message: "something" }, true);
    expect(html).toContain('role="status"');
    expect(html).toContain("Reading your pages");
    expect(html).not.toContain('aria-describedby="import-error"');
  });
});

describe("<ImportRead>", () => {
  it("says the pages were read and nothing more", () => {
    const html = renderToStaticMarkup(
      <ImportRead state={{ ...IDLE_IMPORT, ok: true, importId: "abc" }} />,
    );
    expect(html).toContain("Nothing has become your source yet");
    expect(html).toContain("/sources/imports/abc");
  });
});
