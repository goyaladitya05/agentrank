/**
 * Turning one API refusal into one sentence a merchant can act on.
 *
 * Separate from the server actions that use it because a `"use server"` module may export only
 * async functions, and a pure function of a status and a body is exactly the thing worth testing
 * without a server. Every refusal path through the source workflow ends here.
 *
 * The one rule that matters: this can be handed anything. A gateway between the console and the
 * API can answer 502 with HTML, an empty body or nothing at all, and the body that reaches here
 * is then `null`. Losing a document a merchant just wrote because a proxy answered oddly is worse
 * than any message, so nothing here dereferences a value it has not checked.
 */

export interface Refusal {
  readonly message: string;
  /** True when the API refused because state moved, so the page beside this message is fresh. */
  readonly stale: boolean;
}

export const SOURCE_REFUSALS: Record<string, string> = {
  request_too_large: "This source document is too large to submit. Shorten it and try again.",
  request_too_deeply_nested:
    "This document nests further than a source document ever needs to. Check its structure.",
  length_required: "The console could not send this document. Reload the page and try again.",
  source_request_key_reused:
    "This form has already stored a different source document. Reload to start a new submission, then submit your changes.",
  source_version_conflict:
    "Another process is publishing source snapshots for your merchant. Reload and try again.",
  not_found: "Reload this page and try again.",
  unauthenticated: "Your session has expired. Sign in again to continue.",
};

/**
 * Refusals a merchant import can produce, in the merchant's own terms.
 *
 * The API answers 422 with a sentence for anything about a URL it will not fetch, which is
 * already written for a merchant and passes through the `detail` fallback below. What is mapped
 * here is the small set of codes whose API wording is about a request rather than about a store.
 */
export const IMPORT_REFUSALS: Record<string, string> = {
  request_too_large: "This import names more than the console can send. Remove some pages.",
  request_too_deeply_nested:
    "The console could not send this import. Reload the page and try again.",
  length_required: "The console could not send this import. Reload the page and try again.",
  unauthenticated: "Your session has expired. Sign in again to import your pages.",
};

export const CONFIRM_REFUSALS: Record<string, string> = {
  no_products:
    "No product could be imported from these pages, so there is no source snapshot to create.",
  import_failed: "This import did not finish, so there is nothing to confirm. Run a new one.",
  stock_level_required:
    "State the stock level the evaluation world should hold before creating the snapshot.",
  stock_level_out_of_range: "That stock level is outside the range AgentRank accepts.",
  source_version_conflict:
    "Another process is publishing source snapshots for your merchant. Reload and try again.",
  not_found: "This import is no longer available. Reload to see your imports.",
  unauthenticated: "Your session has expired. Sign in again to create the snapshot.",
};

export const COMPILE_REFUSALS: Record<string, string> = {
  not_found: "This source snapshot is no longer available. Reload to see your current source.",
  unauthenticated: "Your session has expired. Sign in again to run the compiler.",
};

function record(payload: unknown): Record<string, unknown> {
  return typeof payload === "object" && payload !== null && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : {};
}

/**
 * A refusal, in words.
 *
 * A 409 or a 404 means the world moved. A 422 means this request was wrong and the world did not
 * move, so re-reading the page would only hide what has to be fixed.
 */
export function refusal(status: number, payload: unknown, known: Record<string, string>): Refusal {
  const body = record(payload);
  const stale = status === 409 || status === 404;
  const code = typeof body.error === "string" ? body.error : null;
  const mapped = code === null ? undefined : known[code];
  if (mapped !== undefined) return { message: mapped, stale };
  const named = fieldMessages(body.fields);
  if (named.length > 0) {
    return { message: `AgentRank refused this document. ${named.join(" ")}`, stale };
  }
  if (typeof body.detail === "string") return { message: body.detail, stale };
  return { message: `AgentRank refused this request (HTTP ${String(status)}).`, stale };
}

/**
 * Up to three "where: what" sentences out of a schema refusal, and nothing invented.
 *
 * The API answers an unreadable body with a `fields` list of locations and validator sentences,
 * deliberately without the value that failed. Naming the field is the most useful thing a
 * merchant editing a document can be told, and it is the reason this is not flattened into
 * "invalid document".
 */
export function fieldMessages(fields: unknown): string[] {
  if (!Array.isArray(fields)) return [];
  const messages: string[] = [];
  for (const item of fields.slice(0, 3)) {
    const entry = record(item);
    if (typeof entry.message !== "string") continue;
    const where = Array.isArray(entry.location)
      ? entry.location
          .filter((part) => part !== "body")
          .map((part) => String(part))
          .join(".")
      : "";
    messages.push(where === "" ? `${entry.message}.` : `${where}: ${entry.message}.`);
  }
  return messages;
}
