/**
 * The merchant import contracts the console reads, checked field by field.
 *
 * The same rule the rest of this console follows: these values decide what a merchant is told
 * about their own store, so nothing is cast and everything is checked. A decoder either returns a
 * fully typed value or throws a sentence naming the field that disagreed.
 *
 * One thing worth saying about the values here rather than only in the API. Everything in an
 * import came from somebody else's web page, so every string in these types is untrusted content
 * that happens to have passed a schema. None of it is rendered as markup anywhere: React escapes
 * text by default, no view in this console uses `dangerouslySetInnerHTML`, and a URL that arrives
 * here is displayed as text rather than turned into a link, because a link to a merchant page is
 * a navigation this console has no reason to offer and a place a value could become behaviour.
 */

import { DecodeError } from "@/lib/insights/decode";

export type PageKind = "PRODUCT" | "POLICY";
export type ImportState = "COMPLETED" | "FAILED";
export type Availability = "IN_STOCK" | "OUT_OF_STOCK" | "UNKNOWN";
export type Extraction = "STRUCTURED_DATA" | "PAGE_METADATA" | "PAGE_TEXT";

export interface ImportPage {
  readonly url: string;
  readonly kind: PageKind;
  readonly name: string | null;
  readonly retrieved: boolean;
  readonly reason: string | null;
  readonly detail: string | null;
  readonly status_code: number | null;
  readonly byte_count: number;
  readonly content_hash: string | null;
  readonly final_url: string | null;
  readonly redirect_count: number;
  readonly retrieved_at: string | null;
}

export interface ImportVariant {
  readonly sku: string;
  readonly label: string | null;
  readonly price_amount_minor: number;
  readonly currency: string;
  readonly availability: Availability;
  readonly availability_text: string | null;
  readonly inventory_quantity: number | null;
}

export interface ImportProduct {
  readonly external_id: string;
  readonly title: string;
  readonly description: string | null;
  readonly category: string | null;
  readonly source_url: string;
  readonly extraction: Extraction;
  readonly variants: readonly ImportVariant[];
}

export interface ImportPolicy {
  readonly name: string;
  readonly body: string;
  readonly source_url: string;
  readonly truncated: boolean;
}

export interface ImportNote {
  readonly source_url: string;
  readonly code: string;
  readonly detail: string;
  readonly subject: string | null;
}

export interface ImportBlocker {
  readonly code: string;
  readonly detail: string;
}

export interface ImportSummary {
  readonly import_id: string;
  readonly origin: string;
  readonly state: ImportState;
  readonly failure_reason: string | null;
  readonly created_at: string;
  readonly page_count: number;
  readonly retrieved_count: number;
  readonly product_count: number;
  readonly variant_count: number;
  readonly policy_count: number;
  readonly omission_count: number;
  readonly source_snapshot_id: string | null;
  readonly confirmed_at: string | null;
}

export interface SourceImport {
  readonly summary: ImportSummary;
  readonly pages: readonly ImportPage[];
  readonly products: readonly ImportProduct[];
  readonly policies: readonly ImportPolicy[];
  readonly omissions: readonly ImportNote[];
  readonly findings: readonly ImportNote[];
  readonly blockers: readonly ImportBlocker[];
  /**
   * Every variant whose page said nothing about whether it can be bought, by SKU.
   *
   * The snapshot records that honestly and an evaluation world cannot hold it, so this is where a
   * merchant learns which of their own lines they will have to state a stock state for before
   * they can be measured.
   */
  readonly unstated_availability: readonly string[];
  readonly confirmable: boolean;
}

export interface ImportConfirmation {
  readonly import_id: string;
  readonly already_confirmed: boolean;
  readonly created_snapshot: boolean;
  readonly source_snapshot_id: string;
  readonly source_label: string;
}

function object(value: unknown, where: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DecodeError(`${where}: expected an object`);
  }
  return value as Record<string, unknown>;
}

function array(value: unknown, where: string): unknown[] {
  if (!Array.isArray(value)) throw new DecodeError(`${where}: expected an array`);
  return value;
}

function string(value: unknown, where: string): string {
  if (typeof value !== "string") throw new DecodeError(`${where}: expected a string`);
  return value;
}

function nullableString(value: unknown, where: string): string | null {
  return value === null ? null : string(value, where);
}

function integer(value: unknown, where: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) {
    throw new DecodeError(`${where}: expected a whole number`);
  }
  return value;
}

function nullableInteger(value: unknown, where: string): number | null {
  return value === null ? null : integer(value, where);
}

function boolean(value: unknown, where: string): boolean {
  if (typeof value !== "boolean") throw new DecodeError(`${where}: expected true or false`);
  return value;
}

function member<T extends string>(value: unknown, allowed: readonly T[], where: string): T {
  const found = string(value, where);
  if (!(allowed as readonly string[]).includes(found)) {
    throw new DecodeError(`${where}: ${found} is not one this console knows`);
  }
  return found as T;
}

const PAGE_KINDS: readonly PageKind[] = ["PRODUCT", "POLICY"];
const STATES: readonly ImportState[] = ["COMPLETED", "FAILED"];
const AVAILABILITIES: readonly Availability[] = ["IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"];
const EXTRACTIONS: readonly Extraction[] = ["STRUCTURED_DATA", "PAGE_METADATA", "PAGE_TEXT"];

function decodePage(value: unknown, where: string): ImportPage {
  const entry = object(value, where);
  return {
    url: string(entry.url, `${where}.url`),
    kind: member(entry.kind, PAGE_KINDS, `${where}.kind`),
    name: nullableString(entry.name, `${where}.name`),
    retrieved: boolean(entry.retrieved, `${where}.retrieved`),
    reason: nullableString(entry.reason, `${where}.reason`),
    detail: nullableString(entry.detail, `${where}.detail`),
    status_code: nullableInteger(entry.status_code, `${where}.status_code`),
    byte_count: integer(entry.byte_count, `${where}.byte_count`),
    content_hash: nullableString(entry.content_hash, `${where}.content_hash`),
    final_url: nullableString(entry.final_url, `${where}.final_url`),
    redirect_count: integer(entry.redirect_count, `${where}.redirect_count`),
    retrieved_at: nullableString(entry.retrieved_at, `${where}.retrieved_at`),
  };
}

function decodeVariant(value: unknown, where: string): ImportVariant {
  const entry = object(value, where);
  return {
    sku: string(entry.sku, `${where}.sku`),
    label: nullableString(entry.label, `${where}.label`),
    price_amount_minor: integer(entry.price_amount_minor, `${where}.price_amount_minor`),
    currency: string(entry.currency, `${where}.currency`),
    availability: member(entry.availability, AVAILABILITIES, `${where}.availability`),
    availability_text: nullableString(entry.availability_text, `${where}.availability_text`),
    inventory_quantity: nullableInteger(entry.inventory_quantity, `${where}.inventory_quantity`),
  };
}

function decodeProduct(value: unknown, where: string): ImportProduct {
  const entry = object(value, where);
  return {
    external_id: string(entry.external_id, `${where}.external_id`),
    title: string(entry.title, `${where}.title`),
    description: nullableString(entry.description, `${where}.description`),
    category: nullableString(entry.category, `${where}.category`),
    source_url: string(entry.source_url, `${where}.source_url`),
    extraction: member(entry.extraction, EXTRACTIONS, `${where}.extraction`),
    variants: array(entry.variants, `${where}.variants`).map((item, index) =>
      decodeVariant(item, `${where}.variants[${String(index)}]`),
    ),
  };
}

function decodePolicy(value: unknown, where: string): ImportPolicy {
  const entry = object(value, where);
  return {
    name: string(entry.name, `${where}.name`),
    body: string(entry.body, `${where}.body`),
    source_url: string(entry.source_url, `${where}.source_url`),
    truncated: boolean(entry.truncated, `${where}.truncated`),
  };
}

function decodeNote(value: unknown, where: string): ImportNote {
  const entry = object(value, where);
  return {
    source_url: string(entry.source_url, `${where}.source_url`),
    code: string(entry.code, `${where}.code`),
    detail: string(entry.detail, `${where}.detail`),
    subject: nullableString(entry.subject, `${where}.subject`),
  };
}

export function decodeImportSummary(value: unknown, where = "import"): ImportSummary {
  const entry = object(value, where);
  return {
    import_id: string(entry.import_id, `${where}.import_id`),
    origin: string(entry.origin, `${where}.origin`),
    state: member(entry.state, STATES, `${where}.state`),
    failure_reason: nullableString(entry.failure_reason, `${where}.failure_reason`),
    created_at: string(entry.created_at, `${where}.created_at`),
    page_count: integer(entry.page_count, `${where}.page_count`),
    retrieved_count: integer(entry.retrieved_count, `${where}.retrieved_count`),
    product_count: integer(entry.product_count, `${where}.product_count`),
    variant_count: integer(entry.variant_count, `${where}.variant_count`),
    policy_count: integer(entry.policy_count, `${where}.policy_count`),
    omission_count: integer(entry.omission_count, `${where}.omission_count`),
    source_snapshot_id: nullableString(entry.source_snapshot_id, `${where}.source_snapshot_id`),
    confirmed_at: nullableString(entry.confirmed_at, `${where}.confirmed_at`),
  };
}

export function decodeImportHistory(value: unknown): ImportSummary[] {
  return array(value, "imports").map((item, index) =>
    decodeImportSummary(item, `imports[${String(index)}]`),
  );
}

export function decodeSourceImport(value: unknown): SourceImport {
  const entry = object(value, "import");
  return {
    summary: decodeImportSummary(entry.summary, "import.summary"),
    pages: array(entry.pages, "import.pages").map((item, index) =>
      decodePage(item, `import.pages[${String(index)}]`),
    ),
    products: array(entry.products, "import.products").map((item, index) =>
      decodeProduct(item, `import.products[${String(index)}]`),
    ),
    policies: array(entry.policies, "import.policies").map((item, index) =>
      decodePolicy(item, `import.policies[${String(index)}]`),
    ),
    omissions: array(entry.omissions, "import.omissions").map((item, index) =>
      decodeNote(item, `import.omissions[${String(index)}]`),
    ),
    findings: array(entry.findings, "import.findings").map((item, index) =>
      decodeNote(item, `import.findings[${String(index)}]`),
    ),
    blockers: array(entry.blockers, "import.blockers").map((item, index) => {
      const blocker = object(item, `import.blockers[${String(index)}]`);
      return {
        code: string(blocker.code, `import.blockers[${String(index)}].code`),
        detail: string(blocker.detail, `import.blockers[${String(index)}].detail`),
      };
    }),
    unstated_availability: array(entry.unstated_availability, "import.unstated_availability").map(
      (item, index) => string(item, `import.unstated_availability[${String(index)}]`),
    ),
    confirmable: boolean(entry.confirmable, "import.confirmable"),
  };
}

export function decodeImportConfirmation(value: unknown): ImportConfirmation {
  const entry = object(value, "confirmation");
  return {
    import_id: string(entry.import_id, "confirmation.import_id"),
    already_confirmed: boolean(entry.already_confirmed, "confirmation.already_confirmed"),
    created_snapshot: boolean(entry.created_snapshot, "confirmation.created_snapshot"),
    source_snapshot_id: string(entry.source_snapshot_id, "confirmation.source_snapshot_id"),
    source_label: string(entry.source_label, "confirmation.source_label"),
  };
}

/**
 * What one variant's stock says, in the words the merchant's own page justifies and no more.
 *
 * A count is shown only where the page published one, which almost none do. Everything else is
 * the state and nothing beside it, because a number the console rendered next to "In stock" would
 * be a number nobody published.
 */
export function availabilityLabel(
  availability: Availability,
  quantity: number | null = null,
): string {
  if (quantity !== null && quantity > 0) {
    return `${String(quantity)} in stock, published by the page`;
  }
  switch (availability) {
    case "IN_STOCK":
      return "In stock, no quantity published";
    case "OUT_OF_STOCK":
      return "Out of stock";
    default:
      return "Not stated on the page";
  }
}

export function extractionLabel(extraction: Extraction): string {
  switch (extraction) {
    case "STRUCTURED_DATA":
      return "Structured product data";
    case "PAGE_METADATA":
      return "Page metadata tags";
    default:
      return "Page text";
  }
}

/**
 * One imported price, in the currency the page published, with no rounding of its own.
 *
 * The sign is carried separately rather than taken from the whole part. `Math.trunc(-50 / 100)` is
 * negative zero, `String` renders that as "0", and a price between minus one unit and zero would
 * therefore be displayed as positive. The API refuses a negative price, so this is a shape that
 * cannot arrive today; a display helper that silently drops a minus sign is not one to leave in
 * place on the strength of that.
 */
export function formatImportedPrice(amountMinor: number, currency: string): string {
  const exponent = MINOR_UNIT_DIGITS[currency] ?? 2;
  const divisor = 10 ** exponent;
  const magnitude = Math.abs(amountMinor);
  const sign = amountMinor < 0 ? "-" : "";
  const whole = Math.trunc(magnitude / divisor);
  const remainder = magnitude % divisor;
  const fraction = exponent === 0 ? "" : `.${String(remainder).padStart(exponent, "0")}`;
  return `${currency} ${sign}${String(whole)}${fraction}`;
}

/**
 * The currencies whose minor unit is not two digits, for display only.
 *
 * The API decided the integer; this only decides where to draw a decimal point. But it has to
 * agree with the set the API is willing to import, or the review page shows a merchant a price
 * that is a hundred times too small on the one page whose purpose is deciding whether the number
 * is right. This is the whole of `ZERO_DECIMAL` and `THREE_DECIMAL` from
 * `agentrank_api.importer.amounts`, and a currency the API adds there has to be added here.
 * An unlisted currency is drawn with two digits, which is what every other importable one uses.
 */
const MINOR_UNIT_DIGITS: Record<string, number> = {
  BHD: 3,
  BIF: 0,
  CLP: 0,
  DJF: 0,
  GNF: 0,
  IQD: 3,
  ISK: 0,
  JOD: 3,
  JPY: 0,
  KMF: 0,
  KRW: 0,
  KWD: 3,
  LYD: 3,
  OMR: 3,
  PYG: 0,
  RWF: 0,
  TND: 3,
  UGX: 0,
  UYI: 0,
  VND: 0,
  VUV: 0,
  XAF: 0,
  XOF: 0,
  XPF: 0,
};
