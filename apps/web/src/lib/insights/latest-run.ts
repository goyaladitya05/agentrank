import { decodeRunDiagnostics, decodeRunSummaryList } from "@/lib/insights/decode";
import { loadInsight, type InsightsFailure } from "@/lib/insights/load";
import type { RunDiagnostics } from "@/lib/insights/types";
import { latestFinishedRun } from "@/lib/insights/merchant";

export type LatestRunOutcome =
  | { readonly state: "ok"; readonly data: RunDiagnostics }
  | { readonly state: "none" }
  | { readonly state: "failure"; readonly failure: InsightsFailure };

/**
 * The diagnostics of the newest run that actually executed, which is what the merchant
 * Issues surfaces are about. "None" is a first-class answer: a merchant with no finished
 * evaluation has no issues to show and is pointed at running one, not at an error.
 */
export async function loadLatestRunDiagnostics(): Promise<LatestRunOutcome> {
  const runs = await loadInsight("/api/v1/insights/runs?limit=50", decodeRunSummaryList);
  if (!runs.ok) {
    return { state: "failure", failure: runs.failure };
  }
  const latest = latestFinishedRun(runs.data);
  if (latest === null) {
    return { state: "none" };
  }
  const diagnostics = await loadInsight(
    `/api/v1/insights/runs/${encodeURIComponent(latest.run_id)}`,
    decodeRunDiagnostics,
  );
  if (!diagnostics.ok) {
    return { state: "failure", failure: diagnostics.failure };
  }
  return { state: "ok", data: diagnostics.data };
}
