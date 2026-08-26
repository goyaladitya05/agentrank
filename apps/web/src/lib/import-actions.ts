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

import { requireConsoleCredential } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeImportConfirmation, decodeSourceImport } from "@/lib/import";
import type { ConfirmState, ImportState, ImportValues } from "@/lib/import-mutation";
import { CONFIRM_REFUSALS, IMPORT_REFUSALS, refusal } from "@/lib/source-refusal";

/**
 * The most pages one import command may name.
 *
 * The API states the same number and is the one that decides. This gets there first so that a
 * merchant pasting a hundred URLs is told which bound they crossed instead of watching a request
 * fail, and so that a request that was never going to be accepted does not reach a storefront.
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
      "The console could not reach AgentRank, so whether these pages were fetched is unknown. Reload this page; submitting this form again cannot start a second import.",
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
    values: null,
  };
}

function confirmFailed(
  message: string,
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
  };
}

export async function confirmImport(
  importId: string,
  stockLevelRequired: boolean,
  _: ConfirmState,
  formData: FormData,
): Promise<ConfirmState> {
  let stockLevel: number | null = null;
  if (stockLevelRequired) {
    const raw = String(formData.get("stock_level") ?? "").trim();
    if (raw === "") {
      return confirmFailed(
        "State the stock level the evaluation world should hold before creating the snapshot.",
      );
    }
    if (!/^\d+$/.test(raw)) {
      return confirmFailed("The evaluation stock level must be a whole number.");
    }
    stockLevel = Number(raw);
  }

  const credential = await requireConsoleCredential();
  let response: Response;
  try {
    response = await fetch(
      endpoint(`/api/v1/sources/imports/${encodeURIComponent(importId)}/confirm`),
      {
        method: "POST",
        headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json" },
        body: JSON.stringify({ stock_level: stockLevel }),
        cache: "no-store",
      },
    );
  } catch {
    return confirmFailed(
      "The console could not reach AgentRank, so whether a source snapshot was created is unknown. Reload this page; confirming again cannot create a second one.",
      { unknown: true },
    );
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const refused = refusal(response.status, payload, CONFIRM_REFUSALS);
    if (refused.stale) revalidatePath(`/sources/imports/${importId}`);
    return confirmFailed(refused.message, { stale: refused.stale });
  }

  let confirmed;
  try {
    confirmed = decodeImportConfirmation(payload);
  } catch {
    return confirmFailed(
      "AgentRank answered with something this console cannot read. Reload to see your source history.",
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
  };
}
