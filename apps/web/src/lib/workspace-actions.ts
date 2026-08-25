"use server";

/**
 * The console's command to build a merchant's evaluation setup.
 *
 * Unlike a launch this spends nothing: it is deterministic, calls no model, and writes no price,
 * stock level or payment. What it does is publish the benchmark suite the merchant will be
 * measured on and register the isolated catalog it will be measured against, which is worth
 * saying plainly on the page rather than treating as a loading step.
 *
 * Three things are deliberate.
 *
 * The snapshot is carried rather than chosen. The page was rendered against one source snapshot
 * and sends that one back, and the API compares it with the merchant's current one, so a
 * merchant who submitted newer evidence in another tab is refused rather than having a world
 * built from evidence they have replaced.
 *
 * There is no request key. A setup is identified by the merchant, the snapshot and the
 * generation configuration, so a repeat of this command is already the same command and a key
 * would be a second idempotency mechanism for a rule the schema holds.
 *
 * And a response the console never saw is not a failure. A network error leaves the outcome
 * unknown, and saying "it failed" would be a guess that reads as fact. The merchant is told to
 * reload, and pressing the button again cannot build a second setup.
 */

import { revalidatePath } from "next/cache";

import { requireConsoleCredential } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { decodeWorkspace } from "@/lib/workspace";
import type { SetupState } from "@/lib/workspace-mutation";

const REFUSALS: Record<string, string> = {
  merchant_source_unavailable:
    "AgentRank has no record of your merchant information yet, so there is nothing to build an evaluation setup from.",
  source_superseded:
    "Newer merchant information has been published since this page loaded. Reload to build from your current source.",
  no_purchasable_variant:
    "Every variant in your merchant information is out of stock, so there is nothing a buyer could be asked to buy.",
  no_mission_family:
    "AgentRank could not build a benchmark mission from your merchant information. A catalog needs at least one product a buyer could be asked to buy, with a price and stock.",
  source_unreadable:
    "AgentRank could not read your current merchant information as a source document. Submit your source again.",
  source_addresses_the_reader:
    "Your merchant information contains text that addresses whatever reads it rather than describing a product. Edit that field and submit your source again.",
  source_field_too_long:
    "One of your product fields is longer than an evaluation catalog can hold. Shorten it and submit your source again.",
  source_names_another_merchant:
    "That source snapshot was recorded against a different merchant. Reload to see your current source.",
  evaluation_already_pending:
    "An evaluation is already queued or running. It is measuring the setup you have now, so wait for it to finish.",
  run_already_active:
    "A benchmark run is executing against your world. Wait for it to finish before building a new setup.",
  existing_benchmark_world:
    "This merchant already has a benchmark world AgentRank did not generate. Your operator decides which one an evaluation uses.",
  existing_catalog:
    "This merchant already has a product catalog in AgentRank that no evaluation setup produced, so building one is refused. Your operator can see why.",
  not_found: "What this command named is no longer available. Reload to see the current state.",
  unauthenticated: "Your session has expired. Sign in again to build your evaluation setup.",
};

function refusal(status: number, payload: unknown): SetupState {
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
    return { ok: false, message: known, stale, unknown: false, missionCount: null };
  }
  if (typeof body.detail === "string") {
    return { ok: false, message: body.detail, stale, unknown: false, missionCount: null };
  }
  return {
    ok: false,
    message: `AgentRank refused this request (HTTP ${String(status)}).`,
    stale,
    unknown: false,
    missionCount: null,
  };
}

export async function buildEvaluationSetup(
  sourceSnapshotId: string,
  _: SetupState,
): Promise<SetupState> {
  // Resolved before the try. `requireConsoleCredential` redirects by throwing the framework's own
  // control-flow error, and catching that here would turn an expired session into a message
  // about the network instead of a login page.
  const credential = await requireConsoleCredential();
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/api/v1/benchmark/workspace`, {
      method: "POST",
      headers: { Authorization: `Bearer ${credential}`, "Content-Type": "application/json" },
      body: JSON.stringify({ source_snapshot_id: sourceSnapshotId }),
      cache: "no-store",
    });
  } catch {
    return {
      ok: false,
      message:
        "The console could not reach AgentRank, so whether your evaluation setup was built is unknown. Reload this page to see the current state; building again cannot produce a second setup.",
      stale: false,
      unknown: true,
      missionCount: null,
    };
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const failed = refusal(response.status, payload);
    if (failed.stale) refreshed();
    return failed;
  }

  let built;
  try {
    const body =
      typeof payload === "object" && payload !== null && !Array.isArray(payload)
        ? (payload as { workspace?: unknown })
        : {};
    built = decodeWorkspace(body.workspace);
  } catch {
    return {
      ok: false,
      message:
        "AgentRank answered with something this console cannot read. Reload to see the current state.",
      stale: true,
      unknown: true,
      missionCount: null,
    };
  }
  refreshed();
  return {
    ok: true,
    message: null,
    stale: false,
    unknown: false,
    missionCount: built.mission_count,
  };
}

function refreshed(): void {
  revalidatePath("/evaluations");
  revalidatePath("/overview");
}
