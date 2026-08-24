"use server";

/**
 * The console's two source workflow write commands.
 *
 * Both are thin adapters over the API, and both keep the same rules the compiler review and
 * re-evaluation actions established: the merchant credential comes from the server side session,
 * the API decides everything, and refusal codes become sentences here rather than in the API,
 * because the API's other caller is an agent and `request_too_large` is not something to show a
 * shopkeeper.
 *
 * Three things are specific to supplying evidence.
 *
 * The document is parsed here before it is sent. A merchant editing JSON in a textarea will
 * mistype a comma, and "your document is not valid JSON, at this position" is an answer this
 * console can give immediately and a schema refusal from the API cannot give at all.
 *
 * A refusal never empties the editor. What was typed comes back on every failure, including one
 * the console could not classify, because losing a document a merchant just wrote is worse than
 * any message beside it.
 *
 * A response the console never saw is not a failure. A network error leaves the submission in an
 * unknown state, and saying "it failed" would be a guess that reads as fact. The request key
 * makes submitting the same form again safe, so the merchant is told to reload rather than told
 * anything about what happened.
 */

import { revalidatePath } from "next/cache";

import { requireConsoleApiKey } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeCompilerRun } from "@/lib/compiler";
import { decodeSourceSubmission } from "@/lib/source";
import type { CompileState, SourceSubmissionState, SourceValues } from "@/lib/source-mutation";

/**
 * The console refuses a document larger than this before sending it, so a size refusal names a
 * number the merchant can act on rather than arriving as a bare 413.
 *
 * Bytes of the encoded request body, which is what the API bounds. Measuring characters of the
 * editor's pretty printed text would be measuring a different thing twice over: indentation the
 * request does not carry, and multibyte characters as one each. The API enforces the same bound
 * and is the one that decides; this only gets there first.
 */
const MAX_DOCUMENT_BYTES = 128 * 1024;

const REFUSALS: Record<string, string> = {
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

interface Refusal {
  readonly message: string;
  readonly stale: boolean;
}

/**
 * A refusal, in words. A schema refusal carries a list of field locations, which is the most
 * useful thing a merchant editing a document can be told, so the first few are named rather than
 * flattened into "invalid document".
 */
function refusal(status: number, payload: unknown): Refusal {
  const body =
    typeof payload === "object" && payload !== null && !Array.isArray(payload)
      ? (payload as { error?: unknown; detail?: unknown })
      : {};
  // A 409 or a 404 means the world moved. A 422 means this request was wrong and the world did
  // not move, so re-reading the page would only hide what has to be fixed.
  const stale = status === 409 || status === 404;
  const code = typeof body.error === "string" ? body.error : null;
  const known = code === null ? undefined : REFUSALS[code];
  if (known !== undefined) return { message: known, stale };
  const named = fieldMessages((payload as { fields?: unknown }).fields);
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
function fieldMessages(fields: unknown): string[] {
  if (!Array.isArray(fields)) return [];
  const messages: string[] = [];
  for (const item of fields.slice(0, 3)) {
    if (typeof item !== "object" || item === null) continue;
    const entry = item as { location?: unknown; message?: unknown };
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

function refreshed(): void {
  revalidatePath("/sources");
  revalidatePath("/compiler");
  revalidatePath("/overview");
}

function failed(
  message: string,
  values: SourceValues,
  options: { stale?: boolean; unknown?: boolean } = {},
): SourceSubmissionState {
  return {
    ok: false,
    message,
    stale: options.stale ?? false,
    unknown: options.unknown ?? false,
    snapshotId: null,
    createdSnapshot: false,
    values,
  };
}

export async function submitSource(
  requestKey: string,
  _: SourceSubmissionState,
  formData: FormData,
): Promise<SourceSubmissionState> {
  const text = String(formData.get("document") ?? "");
  const values: SourceValues = { document: text };

  if (text.trim() === "") {
    return failed("Enter your source document before submitting it.", values);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    const detail = error instanceof Error ? error.message : "it could not be read";
    return failed(`This is not valid JSON: ${detail}`, values);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return failed("A source document is a JSON object with products and policy text.", values);
  }
  if ("request_key" in parsed) {
    return failed(
      "Remove request_key from the document. The console supplies it with each submission.",
      values,
    );
  }

  // Measured on the body that is actually sent, which is what the API bounds.
  const encoded = JSON.stringify({ ...parsed, request_key: requestKey });
  const size = new TextEncoder().encode(encoded).length;
  if (size > MAX_DOCUMENT_BYTES) {
    return failed(
      `This document is ${String(size)} bytes. The limit is ${String(MAX_DOCUMENT_BYTES)}.`,
      values,
    );
  }

  // Resolved before the try. `requireConsoleApiKey` redirects to sign in by throwing the
  // framework's own control-flow error, and catching that here would turn an expired session
  // into a message about the network instead of a login page.
  const apiKey = await requireConsoleApiKey();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/api/v1/sources`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: encoded,
      cache: "no-store",
    });
  } catch {
    return failed(
      "The console could not reach AgentRank, so whether this document was stored is unknown. Reload this page to see your current source; submitting this form again cannot create a second snapshot.",
      values,
      { unknown: true },
    );
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const refused = refusal(response.status, payload);
    if (refused.stale) refreshed();
    return failed(refused.message, values, { stale: refused.stale });
  }

  let submitted;
  try {
    submitted = decodeSourceSubmission(payload);
  } catch {
    return failed(
      "AgentRank answered with something this console cannot read. Reload to see your current source.",
      values,
      { stale: true, unknown: true },
    );
  }
  refreshed();
  revalidatePath(`/sources/${submitted.snapshot.source_snapshot_id}`);
  return {
    ok: true,
    message: null,
    stale: false,
    unknown: false,
    snapshotId: submitted.snapshot.source_snapshot_id,
    createdSnapshot: submitted.created_snapshot,
    values: null,
  };
}

const COMPILE_REFUSALS: Record<string, string> = {
  not_found: "This source snapshot is no longer available. Reload to see your current source.",
  unauthenticated: "Your session has expired. Sign in again to run the compiler.",
};

const IDLE_FAILURE = {
  ok: false as const,
  runId: null,
  runStatus: null,
  pendingReviews: null,
  published: false,
};

export async function startCompilerRun(
  sourceSnapshotId: string,
  _: CompileState,
): Promise<CompileState> {
  const apiKey = await requireConsoleApiKey();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/api/v1/compiler/runs`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ source_snapshot_id: sourceSnapshotId }),
      cache: "no-store",
    });
  } catch {
    return {
      ...IDLE_FAILURE,
      message:
        "The console could not reach AgentRank, so whether the compiler ran is unknown. Reload this page; asking again cannot produce a second run of this snapshot.",
      stale: false,
      unknown: true,
    };
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const body =
      typeof payload === "object" && payload !== null && !Array.isArray(payload)
        ? (payload as { error?: unknown; detail?: unknown })
        : {};
    const code = typeof body.error === "string" ? body.error : null;
    const stale = response.status === 409 || response.status === 404;
    const known = code === null ? undefined : COMPILE_REFUSALS[code];
    const message =
      known ??
      (typeof body.detail === "string"
        ? body.detail
        : `AgentRank refused this request (HTTP ${String(response.status)}).`);
    if (stale) revalidatePath(`/sources/${sourceSnapshotId}`);
    return { ...IDLE_FAILURE, message, stale, unknown: false };
  }

  let run;
  try {
    run = decodeCompilerRun(payload);
  } catch {
    return {
      ...IDLE_FAILURE,
      message:
        "AgentRank answered with something this console cannot read. Reload to see this snapshot.",
      stale: true,
      unknown: true,
    };
  }
  refreshed();
  revalidatePath(`/sources/${sourceSnapshotId}`);
  return {
    ok: true,
    message: null,
    stale: false,
    unknown: false,
    runId: run.run_id,
    runStatus: run.status,
    // What is still unanswered, counted the same way the run page counts it. A candidate the
    // compiler accepted needs nothing, and one already reviewed is settled.
    pendingReviews: run.candidates.filter(
      (candidate) => candidate.state === "REVIEW_REQUIRED" && candidate.review === null,
    ).length,
    published: run.readiness.published_representation_id !== null,
  };
}
