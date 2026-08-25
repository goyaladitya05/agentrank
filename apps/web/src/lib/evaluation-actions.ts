"use server";

/**
 * The console's one benchmark write command.
 *
 * A launch spends model quota and takes as long as a suite takes, so this is the most expensive
 * thing a merchant can do here and it is treated accordingly.
 *
 * Four things are deliberate.
 *
 * The purpose is carried rather than chosen. The server decides whether this merchant is asking
 * for a first evaluation or a re-evaluation, and this sends back the answer the page was
 * rendered from, so a merchant who published in another tab is refused rather than launching a
 * measurement of something they have stopped presenting.
 *
 * The request key comes from the rendered form rather than from this action. One preflight
 * render is one launch: submitting the same form twice, or retrying after a lost response,
 * repeats the key and the API answers with the launch that key already produced. Opening the
 * page again is a new key and therefore a deliberate second launch.
 *
 * A response the console never saw is not a failure. A network error leaves the launch in an
 * unknown state, and saying "it failed" would be a guess that reads as fact. The merchant is
 * told to reload, and retrying the same form cannot produce a second run.
 *
 * And refusal codes become sentences here rather than in the API, for the same reason compiler
 * refusals do: the API's other caller is an agent, and `representation_superseded` is not
 * something to show a shopkeeper.
 */

import { revalidatePath } from "next/cache";

import { requireConsoleApiKey } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeEvaluationLaunch, type EvaluationPurpose } from "@/lib/evaluation";
import type { LaunchState } from "@/lib/evaluation-mutation";

const REFUSALS: Record<string, string> = {
  no_published_representation:
    "Publish an agent-ready representation before requesting a re-evaluation.",
  representation_lineage_unreadable:
    "AgentRank could not read the compiler run behind your published representation. Contact your operator; this is not something you can fix from here.",
  representation_superseded:
    "A newer agent-ready representation has been published since this page loaded. Reload to evaluate the current one.",
  preflight_superseded:
    "What this evaluation would run has changed since this page loaded. Reload to see what would be evaluated now.",
  evaluation_already_pending:
    "An evaluation is already queued or running for your merchant. Wait for it to finish before starting another.",
  evaluation_request_key_reused:
    "This form has already launched a different evaluation. Reload and try again.",
  evaluation_purpose_superseded:
    "What AgentRank would evaluate for your merchant has changed since this page loaded. Reload to see what would be evaluated now.",
  initial_evaluation_names_no_representation:
    "A first evaluation measures your merchant as it is now, so it cannot name a published representation. Reload and try again.",
  merchant_source_unavailable:
    "AgentRank has no record of your merchant information yet, so there is nothing to evaluate you against.",
  run_already_active:
    "A benchmark run is already executing against your world. Only one run may own it at a time.",
  benchmark_suite_unavailable:
    "No benchmark suite is published for your merchant, so there is nothing to run.",
  benchmark_world_unregistered:
    "Your merchant has no registered benchmark world, so a run has no catalog to be put back to.",
  not_found: "What this evaluation named is no longer available. Reload to see the current state.",
  unauthenticated: "Your session has expired. Sign in again to request an evaluation.",
};

function refusal(status: number, payload: unknown): LaunchState {
  const body =
    typeof payload === "object" && payload !== null && !Array.isArray(payload)
      ? (payload as { error?: unknown; detail?: unknown })
      : {};
  const code = typeof body.error === "string" ? body.error : null;
  // A 409 or a 404 means the world moved while the merchant was reading it. A 422 means this
  // request was wrong and the world did not move.
  const stale = status === 409 || status === 404;
  const known = code === null ? undefined : REFUSALS[code];
  if (known !== undefined) {
    return { ok: false, message: known, stale, unknown: false, launchId: null };
  }
  if (typeof body.detail === "string") {
    return { ok: false, message: body.detail, stale, unknown: false, launchId: null };
  }
  return {
    ok: false,
    message: `AgentRank refused this request (HTTP ${String(status)}).`,
    stale,
    unknown: false,
    launchId: null,
  };
}

export async function requestEvaluation(
  purpose: EvaluationPurpose,
  representationId: string | null,
  requestKey: string,
  planDigest: string,
  _: LaunchState,
): Promise<LaunchState> {
  // Resolved before the try. `requireConsoleApiKey` redirects by throwing the framework's own
  // control-flow error, and catching that here would turn an expired session into a message
  // about the network instead of a login page.
  const apiKey = await requireConsoleApiKey();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/api/v1/benchmark/evaluations`, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        purpose,
        representation_id: representationId,
        request_key: requestKey,
        plan_digest: planDigest,
      }),
      cache: "no-store",
    });
  } catch {
    return {
      ok: false,
      message:
        "The console could not reach AgentRank, so whether this launch was accepted is unknown. Reload this page to see the current state; submitting again cannot start a second run.",
      stale: false,
      unknown: true,
      launchId: null,
    };
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const failed = refusal(response.status, payload);
    if (failed.stale) refreshed();
    return failed;
  }

  let launched;
  try {
    launched = decodeEvaluationLaunch(payload);
  } catch {
    return {
      ok: false,
      message:
        "AgentRank answered with something this console cannot read. Reload to see the current state.",
      stale: true,
      unknown: true,
      launchId: null,
    };
  }
  refreshed();
  return {
    ok: true,
    message: null,
    stale: false,
    unknown: false,
    launchId: launched.launch_id,
  };
}

function refreshed(): void {
  revalidatePath("/evaluations");
  revalidatePath("/overview");
}
