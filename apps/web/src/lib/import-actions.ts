"use server";

/**
 * The console's two merchant import write commands.
 *
 * Both are thin adapters over the API, and both keep the rules the source workflow established:
 * the merchant credential comes from the server side session, the API decides everything, and
 * refusal codes become sentences rather than being shown raw.
 *
 * Two things are specific to importing.
 *
 * **The URLs are assembled here and validated there.** This turns a textarea of lines into the
 * list the API expects and refuses the obvious mistakes, an empty form and more pages than the
 * API accepts, before making a request that would fetch somebody's website. Everything that
 * decides whether a URL may actually be fetched is the API's, and none of it is duplicated here:
 * a check written in a browser-facing action is a check the API's other callers would not have,
 * and this one in particular is a security boundary.
 *
 * **A response the console never saw is not a failure.** A network error leaves the import in an
 * unknown state, and saying "it failed" would be a guess that reads as fact. The request key makes
 * submitting the same form again safe, so the merchant is told to reload.
 */

import { revalidatePath } from "next/cache";

import { consoleCredential, requireConsoleCredential } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeImportConfirmation, decodeSourceImport } from "@/lib/import";
import type { ConfirmState, ConfirmValues, ImportState, ImportValues } from "@/lib/import-mutation";
import { CONFIRM_REFUSALS, IMPORT_REFUSALS, refusal } from "@/lib/source-refusal";

/**
 * The most pages one import command may name.
 *
 * The API states the same number, as `MAX_IMPORT_PAGES` in `agentrank_api.importer.service`, and
 * is the one that decides. This gets there first so that a merchant pasting a hundred URLs is told
 * which bound they crossed instead of watching a request fail, and so that a request that was
 * never going to be accepted does not reach a storefront. The sentence in `RunImport.tsx` states
 * the same number in words, so a change to the API constant is three edits and drift shows up as
 * a merchant refused for a bound the console told them was different.
 */
const MAX_PAGES = 12;

interface RequestedPage {
  readonly url: string;
  readonly kind: "PRODUCT" | "POLICY";
  readonly name?: string;
}

function endpoint(path: string): string {
  return `${apiBaseUrl().replace(/\/+$/, "")}${path}`;
}

function failed(
  message: string,
  values: ImportValues,
  options: { stale?: boolean; unknown?: boolean } = {},
): ImportState {
  return {
    ok: false,
    message,
    stale: options.stale ?? false,
    unknown: options.unknown ?? false,
    importId: null,
    completed: false,
    values,
  };
}

/** One textarea of URLs, one per line, with blank lines and stray whitespace dropped. */
function lines(raw: string): string[] {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

function collect(values: ImportValues): RequestedPage[] {
  const pages: RequestedPage[] = [];
  for (const url of lines(`${values.storefront}\n${values.products}`)) {
    pages.push({ url, kind: "PRODUCT" });
  }
  for (const [name, raw] of [
    ["returns", values.returns],
    ["warranty", values.warranty],
    ["shipping", values.shipping],
  ] as const) {
    const [url] = lines(raw);
    if (url !== undefined) pages.push({ url, kind: "POLICY", name });
  }
  return pages;
}

function submitted(formData: FormData): ImportValues {
  return {
    storefront: String(formData.get("storefront") ?? ""),
    products: String(formData.get("products") ?? ""),
    returns: String(formData.get("returns") ?? ""),
    warranty: String(formData.get("warranty") ?? ""),
    shipping: String(formData.get("shipping") ?? ""),
  };
}

export async function runImport(
  requestKey: string,
  _: ImportState,
  formData: FormData,
): Promise<ImportState> {
  const values = submitted(formData);
  const pages = collect(values);

  if (pages.length === 0) {
    return failed("Enter at least one public page URL from your store.", values);
  }
  if (pages.length > MAX_PAGES) {
    return failed(
      `An import may name at most ${String(MAX_PAGES)} pages. This one names ${String(pages.length)}.`,
      values,
    );
  }

  // A lapsed cookie is answered here rather than by a redirect, for the reason the source editor
  // gives: a redirect is a full navigation and would take the merchant's typed URLs with it.
  if ((await consoleCredential()) === null) {
    return failed(
      "Your session has expired. Sign in again in another tab, then submit this form again: the pages you typed are still here.",
      values,
    );
  }

  // Resolved before the try. `requireConsoleCredential` redirects to sign in by throwing the
  // framework's own control-flow error, and catching that here would turn an expired session into
  // a message about the network instead of a login page.
  const credential = await requireConsoleCredential();
  let response: Response;
  try {
    response = await fetch(endpoint("/api/v1/sources/imports"), {
      method: "POST",
      headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json" },
      body: JSON.stringify({ request_key: requestKey, pages }),
      cache: "no-store",
    });
  } catch {
    return failed(
      "The console could not reach AgentRank, so whether these pages were fetched is unknown. Submit this form again without reloading: it repeats the same request and cannot fetch your storefront twice. Reloading starts a new import, which would fetch it again.",
      values,
      { unknown: true },
    );
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const refused = refusal(response.status, payload, IMPORT_REFUSALS);
    return failed(refused.message, values, { stale: refused.stale });
  }

  let found;
  try {
    found = decodeSourceImport(payload);
  } catch {
    return failed(
      "AgentRank answered with something this console cannot read. Reload to see your imports.",
      values,
      { stale: true, unknown: true },
    );
  }
  revalidatePath("/sources");
  revalidatePath("/sources/import");
  return {
    ok: true,
    message: null,
    stale: false,
    unknown: false,
    importId: found.summary.import_id,
    completed: found.summary.state === "COMPLETED",
    values: null,
  };
}

function confirmFailed(
  message: string,
  values: ConfirmValues,
  options: { stale?: boolean; unknown?: boolean } = {},
): ConfirmState {
  return {
    ok: false,
    message,
    stale: options.stale ?? false,
    unknown: options.unknown ?? false,
    snapshotId: null,
    sourceLabel: null,
    createdSnapshot: false,
    alreadyConfirmed: false,
    values,
  };
}

/**
 * Turn one reviewed import into a source snapshot.
 *
 * The merchant states nothing. A source variant holds the availability a storefront publishes, so
 * every fact in the snapshot this creates came off the merchant's own pages; the form used to ask
 * for a stock level because a source variant needed an exact count and no public page publishes
 * one, and asking for that number was the last place this workflow put a figure nobody had
 * published into a merchant's own history.
 */
export async function confirmImport(
  importId: string,
  _: ConfirmState,
  __: FormData,
): Promise<ConfirmState> {
  const values: ConfirmValues = null;
  const credential = await requireConsoleCredential();
  let response: Response;
  try {
    response = await fetch(
      endpoint(`/api/v1/sources/imports/${encodeURIComponent(importId)}/confirm`),
      {
        method: "POST",
        headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json" },
        body: JSON.stringify({}),
        cache: "no-store",
      },
    );
  } catch {
    return confirmFailed(
      "The console could not reach AgentRank, so whether a source snapshot was created is unknown. Reload this page; confirming again cannot create a second one.",
      values,
      { unknown: true },
    );
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const refused = refusal(response.status, payload, CONFIRM_REFUSALS);
    if (refused.stale) revalidatePath(`/sources/imports/${importId}`);
    return confirmFailed(refused.message, values, { stale: refused.stale });
  }

  let confirmed;
  try {
    confirmed = decodeImportConfirmation(payload);
  } catch {
    return confirmFailed(
      "AgentRank answered with something this console cannot read. Reload to see your source history.",
      values,
      { stale: true, unknown: true },
    );
  }
  revalidatePath("/sources");
  revalidatePath("/overview");
  revalidatePath("/evaluations");
  revalidatePath(`/sources/imports/${importId}`);
  revalidatePath(`/sources/${confirmed.source_snapshot_id}`);
  return {
    ok: true,
    message: null,
    stale: false,
    unknown: false,
    snapshotId: confirmed.source_snapshot_id,
    sourceLabel: confirmed.source_label,
    createdSnapshot: confirmed.created_snapshot,
    alreadyConfirmed: confirmed.already_confirmed,
    values: null,
  };
}
