"use server";

/**
 * The console's four compiler write commands.
 *
 * Every one of them is a thin adapter. The merchant credential comes from the server side
 * session, the API decides everything, and the answer that comes back is the authoritative run
 * state rather than anything assembled here.
 *
 * Two things are deliberate and are the reason this file is not three lines long.
 *
 * The API answers a refused command with a stable machine readable code and a detail written for
 * an agent, not for a shopkeeper: `candidate_already_reviewed` carries the candidate target as
 * its detail. Showing that raw is showing a merchant a compiler identifier, so codes are mapped
 * to sentences here and only an unmapped answer falls back to the API's own words.
 *
 * A refusal caused by state, rather than by the request, means the page is out of date. Those
 * revalidate before returning, so the merchant reads the message beside the state that produced
 * it rather than beside the state they were looking at when they submitted.
 */

import { revalidatePath } from "next/cache";

import { requireConsoleApiKey } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeCompilerRun } from "@/lib/compiler";
import type { CompilerMutationState, CorrectionValues } from "@/lib/compiler-mutation";

const REFUSALS: Record<string, string> = {
  candidate_already_reviewed:
    "Someone already reviewed this fact. The decision now shown is the one that counts.",
  candidate_does_not_require_review: "This fact no longer needs a review decision.",
  candidate_requires_correction:
    "The compiler could not choose between the values in your source, so this fact cannot be accepted as proposed. Enter the correct value instead.",
  compiler_run_already_published:
    "This run has already been published. Its reviewed facts can no longer change.",
  compiler_review_required: "Some facts still need a decision before this run can be published.",
  compiler_run_not_completed: "This compiler run did not complete, so there is nothing to publish.",
  compiler_representation_conflict:
    "The representation for this run could not be written. Refresh and try again.",
  not_found: "This fact is no longer available. Refresh to see the current run.",
  unauthenticated: "Your session has expired. Sign in again to continue reviewing.",
};

interface Refusal {
  readonly message: string;
  readonly stale: boolean;
}

function refusal(status: number, payload: unknown): Refusal {
  const body =
    typeof payload === "object" && payload !== null && !Array.isArray(payload)
      ? (payload as { error?: unknown; detail?: unknown })
      : {};
  const code = typeof body.error === "string" ? body.error : null;
  const known = code === null ? undefined : REFUSALS[code];
  // A 409 or a 404 means the world moved. A 422 means this request was wrong and the world did
  // not move, so re-reading the page would only hide the field the merchant has to fix.
  const stale = status === 409 || status === 404;
  if (known !== undefined) return { message: known, stale };
  if (typeof body.detail === "string") return { message: body.detail, stale };
  return {
    message: `The compiler refused this command (HTTP ${String(status)}). Refresh to see the current review state.`,
    stale,
  };
}

async function command(path: string, body?: unknown): Promise<Refusal | null> {
  // Resolved before the try. `requireConsoleApiKey` redirects to sign in by throwing the
  // framework's own control-flow error, and catching that here would turn an expired session
  // into a message about the network instead of a login page.
  const apiKey = await requireConsoleApiKey();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: "no-store",
    });
  } catch {
    return {
      message: "The console could not reach the API. Your entry is kept, so you can try again.",
      stale: false,
    };
  }
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) return refusal(response.status, payload);
  try {
    decodeCompilerRun(payload);
  } catch {
    return {
      message: "The compiler answered with something this console cannot read. Refresh and retry.",
      stale: true,
    };
  }
  return null;
}

function refreshed(runId: string): void {
  revalidatePath(`/compiler/runs/${runId}`);
  revalidatePath("/compiler");
  revalidatePath("/overview");
}

function correctionValue(kind: string, raw: string): string | number | boolean | null {
  if (kind === "INTEGER" || kind === "MEASUREMENT") {
    const parsed = Number(raw);
    return raw.trim() !== "" && Number.isFinite(parsed) ? parsed : null;
  }
  if (kind === "BOOLEAN") {
    return raw === "true" ? true : raw === "false" ? false : null;
  }
  return raw.trim() === "" ? null : raw;
}

export async function reviewCandidate(
  runId: string,
  candidateId: string,
  _: CompilerMutationState,
  formData: FormData,
): Promise<CompilerMutationState> {
  const decision = String(formData.get("decision") ?? "");
  const entered: CorrectionValues = {
    value: String(formData.get("value") ?? ""),
    provenanceField: String(formData.get("provenance_field") ?? ""),
    provenanceExcerpt: String(formData.get("provenance_excerpt") ?? ""),
  };

  if (decision === "accept" || decision === "reject") {
    const failed = await command(
      `/api/v1/compiler/candidates/${encodeURIComponent(candidateId)}/${decision}`,
    );
    if (failed !== null) {
      if (failed.stale) refreshed(runId);
      return { ok: false, message: failed.message, stale: failed.stale, values: null };
    }
    refreshed(runId);
    return { ok: true, message: null, stale: false, values: null };
  }

  if (decision !== "correct") {
    return {
      ok: false,
      message: "Unknown compiler review action.",
      stale: false,
      values: entered,
    };
  }

  const value = correctionValue(String(formData.get("kind") ?? ""), entered.value);
  if (value === null) {
    return {
      ok: false,
      message: "Enter a corrected value of the type this fact expects.",
      stale: false,
      values: entered,
    };
  }
  if (entered.provenanceField.trim() === "") {
    return {
      ok: false,
      message: "Name the source field this correction comes from.",
      stale: false,
      values: entered,
    };
  }
  const failed = await command(
    `/api/v1/compiler/candidates/${encodeURIComponent(candidateId)}/correct`,
    {
      value,
      provenance_field: entered.provenanceField,
      provenance_excerpt: entered.provenanceExcerpt === "" ? null : entered.provenanceExcerpt,
    },
  );
  if (failed !== null) {
    if (failed.stale) refreshed(runId);
    return { ok: false, message: failed.message, stale: failed.stale, values: entered };
  }
  refreshed(runId);
  return { ok: true, message: null, stale: false, values: null };
}

export async function publishRun(
  runId: string,
  _: CompilerMutationState,
): Promise<CompilerMutationState> {
  const failed = await command(`/api/v1/compiler/runs/${encodeURIComponent(runId)}/publish`);
  if (failed !== null) {
    if (failed.stale) refreshed(runId);
    return { ok: false, message: failed.message, stale: failed.stale, values: null };
  }
  refreshed(runId);
  return { ok: true, message: null, stale: false, values: null };
}
