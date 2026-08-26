"use server";

/**
 * The console's two source workflow write commands.
 *
 * Both are thin adapters over the API, and both keep the same rules the compiler review and
 * re-evaluation actions established: the merchant credential comes from the server side session,
 * the API decides everything, and refusal codes become sentences rather than being shown raw,
 * because the API's other caller is an agent and `request_too_large` is not something to show a
 * shopkeeper. That translation lives in `@/lib/source-refusal`, which is a pure function of a
 * status and a body and is tested as one.
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

import { requireConsoleCredential } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeCompilerRun } from "@/lib/compiler";
import { decodeSourceSubmission } from "@/lib/source";
import { COMPILE_REFUSALS, SOURCE_REFUSALS, refusal } from "@/lib/source-refusal";
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

/**
 * Submit an edited source document.
 *
 * `baseSnapshotId` is the snapshot this editor was prefilled from, carried back so the server can
 * refuse a submission whose base has moved. The editor takes a whole document and gives back a
 * whole document, so a merchant with this page open in one tab who confirmed an import in
 * another would otherwise write the older body over the top: history keeps both, their current
 * source silently loses what the import added, and the console reports success.
 */
export async function submitSource(
  requestKey: string,
  baseSnapshotId: string | null,
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
  if ("request_key" in parsed || "base_source_snapshot_id" in parsed) {
    return failed(
      "Remove request_key and base_source_snapshot_id from the document. The console supplies both with each submission.",
      values,
    );
  }

  // Measured on the body that is actually sent, which is what the API bounds.
  const encoded = JSON.stringify({
    ...parsed,
    request_key: requestKey,
    base_source_snapshot_id: baseSnapshotId,
  });
  const size = new TextEncoder().encode(encoded).length;
  if (size > MAX_DOCUMENT_BYTES) {
    return failed(
      `This document is ${String(size)} bytes. The limit is ${String(MAX_DOCUMENT_BYTES)}.`,
      values,
    );
  }

  // Resolved before the try. `requireConsoleCredential` redirects to sign in by throwing the
  // framework's own control-flow error, and catching that here would turn an expired session
  // into a message about the network instead of a login page.
  const credential = await requireConsoleCredential();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/api/v1/sources`, {
      method: "POST",
      headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json" },
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
    const refused = refusal(response.status, payload, SOURCE_REFUSALS);
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
  const credential = await requireConsoleCredential();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/api/v1/compiler/runs`, {
      method: "POST",
      headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json" },
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
    const refused = refusal(response.status, payload, COMPILE_REFUSALS);
    if (refused.stale) revalidatePath(`/sources/${sourceSnapshotId}`);
    return { ...IDLE_FAILURE, message: refused.message, stale: refused.stale, unknown: false };
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
