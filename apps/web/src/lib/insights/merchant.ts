/**
 * Product derivations for the merchant surfaces: the overview's one next action, the
 * grouping the Issues page presents, the history stream and the before/after summary.
 *
 * The same rule as `summary.ts`: the backend owns every fact and its meaning, and nothing
 * here reinterprets ownership, actionability or comparability. This module only decides
 * which of the backend's answers the merchant product leads with, which is a presentation
 * decision and is tested as one.
 */

import type { EvaluationLaunch, EvaluationPreflight, RunComparison } from "@/lib/evaluation";
import type { MerchantFinding, MerchantOverview, RunSummary } from "@/lib/insights/types";

/** The newest run whose numbers describe something that actually executed. */
export function latestFinishedRun(runs: readonly RunSummary[]): RunSummary | null {
  return runs.find((run) => run.status === "COMPLETED" || run.status === "ABORTED") ?? null;
}

export interface GroupedFindings {
  /** Findings the merchant should act on or decide about, most severe first. */
  readonly needsAttention: readonly MerchantFinding[];
  /** Provider, system and informational findings. Never presented as merchant work. */
  readonly noActionRequired: readonly MerchantFinding[];
}

const SEVERITY_ORDER: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

/**
 * The two piles the Issues page shows. The split follows the API's actionability verbatim:
 * a finding is the merchant's exactly when the diagnostics engine said so, and a provider
 * failure can never migrate into the attention pile through presentation logic here.
 */
export function groupFindings(findings: readonly MerchantFinding[]): GroupedFindings {
  const needsAttention = findings
    .filter(
      (finding) =>
        finding.actionability === "MERCHANT_ACTION" || finding.actionability === "REVIEW_REQUIRED",
    )
    .toSorted((a, b) => (SEVERITY_ORDER[a.severity] ?? 4) - (SEVERITY_ORDER[b.severity] ?? 4));
  const noActionRequired = findings.filter(
    (finding) =>
      finding.actionability !== "MERCHANT_ACTION" && finding.actionability !== "REVIEW_REQUIRED",
  );
  return { needsAttention, noActionRequired };
}

export interface NextAction {
  /** Stable identifier for tests and for choosing secondary links. */
  readonly kind:
    | "import-store"
    | "prepare-evaluation"
    | "run-first-evaluation"
    | "evaluation-in-progress"
    | "review-issues"
    | "review-fixes"
    | "measure-again"
    | "all-clear";
  readonly label: string;
  readonly href: string;
  readonly body: string;
}

/**
 * The one action the overview leads with, decided from the merchant's whole state.
 *
 * The order is the merchant journey: get a store in, get it measured, act on what the
 * measurement found, approve the fixes, measure again. A running evaluation preempts
 * everything because every other action would be acting on evidence about to be superseded.
 */
export function nextAction(
  overview: MerchantOverview,
  preflight: EvaluationPreflight | null,
): NextAction {
  const finished = latestFinishedRun(overview.runs);
  const hasSource = overview.representation_state.source_snapshot_id !== null;

  if (preflight !== null && preflight.pending_launch_id !== null) {
    return {
      kind: "evaluation-in-progress",
      label: "Follow the evaluation",
      href: `/evaluations/${encodeURIComponent(preflight.pending_launch_id)}`,
      body: "An evaluation you requested is queued or running. Results appear when it finishes.",
    };
  }

  if (!hasSource && finished === null) {
    return {
      kind: "import-store",
      label: "Import your store",
      href: "/sources/import",
      body: "AgentRank reads your own public product pages. Nothing about your live store changes.",
    };
  }

  if (finished === null) {
    if (preflight !== null && preflight.launchable) {
      return {
        kind: "run-first-evaluation",
        label: "Run your first evaluation",
        href: "/evaluations",
        body: "AI shopping agents attempt realistic purchases against your store and AgentRank records what happens.",
      };
    }
    return {
      kind: "prepare-evaluation",
      label: "Prepare your evaluation",
      href: "/evaluations",
      body: "Your store is imported. One more step builds the shopping scenarios AgentRank will test.",
    };
  }

  const attention = groupFindings(overview.top_findings).needsAttention.length;
  const fixes = overview.representation_state.review_required_facts;

  if (attention > 0 && fixes === 0) {
    return {
      kind: "review-issues",
      label: "Review issues",
      href: "/issues",
      body:
        attention === 1
          ? "1 finding on your latest evaluation needs your attention."
          : `${String(attention)} findings on your latest evaluation need your attention.`,
    };
  }

  if (fixes > 0) {
    return {
      kind: "review-fixes",
      label: fixes === 1 ? "Review 1 fix" : `Review ${String(fixes)} fixes`,
      href: "/fixes",
      body: "AgentRank found facts that could make your store easier for shopping agents to understand. Each one waits for your decision.",
    };
  }

  if (preflight !== null && preflight.purpose === "REEVALUATION" && preflight.launchable) {
    return {
      kind: "measure-again",
      label: "Measure again",
      href: "/evaluations",
      body: "Your published fixes have not been measured yet. A re-evaluation shows what changed.",
    };
  }

  return {
    kind: "all-clear",
    label: "See your history",
    href: "/history",
    body: "Nothing needs your action right now. Your evaluations and changes are in your history.",
  };
}

/** One event in the merchant's history stream. */
export interface MerchantEvent {
  /** Sort key and displayed time. ISO from the API, always present. */
  readonly at: string;
  readonly kind: "evaluation" | "source" | "fixes";
  readonly title: string;
  readonly detail: string;
  readonly href: string | null;
  readonly status: { readonly label: string; readonly tone: "ok" | "warn" | "info" | "neutral" };
}

export interface HistorySnapshot {
  readonly source_snapshot_id: string;
  readonly source_label: string;
  readonly origin: string;
  readonly created_at: string;
  readonly product_count: number;
  readonly variant_count: number;
}

export interface HistoryCompilerRun {
  readonly run_id: string;
  readonly source_label: string;
  readonly status: string;
  readonly created_at: string;
  readonly review_required_count: number;
  readonly reviewed_count: number;
  readonly published_representation_id: string | null;
}

/**
 * The merchant history: evaluations, source updates and fix batches in one stream,
 * newest first. Raw benchmark runs are deliberately not the unit here; an evaluation the
 * merchant asked for is, and its run is reachable from its detail page.
 */
export function composeHistory(
  launches: readonly EvaluationLaunch[],
  snapshots: readonly HistorySnapshot[],
  compilerRuns: readonly HistoryCompilerRun[],
): readonly MerchantEvent[] {
  const events: MerchantEvent[] = [];

  for (const launch of launches) {
    events.push({
      at: launch.requested_at,
      kind: "evaluation",
      title: launch.purpose === "INITIAL" ? "First evaluation" : "Re-evaluation",
      detail: launchDetail(launch),
      href: `/evaluations/${encodeURIComponent(launch.launch_id)}`,
      status: launchTone(launch),
    });
  }

  for (const snapshot of snapshots) {
    events.push({
      at: snapshot.created_at,
      kind: "source",
      title: snapshot.origin === "MERCHANT_IMPORT" ? "Store imported" : "Source updated",
      detail: `${snapshot.source_label}: ${String(snapshot.product_count)} products, ${String(snapshot.variant_count)} variants.`,
      href: `/sources/${encodeURIComponent(snapshot.source_snapshot_id)}`,
      status: { label: "Recorded", tone: "neutral" },
    });
  }

  for (const run of compilerRuns) {
    if (run.status !== "COMPLETED" && run.published_representation_id === null) {
      continue;
    }
    const published = run.published_representation_id !== null;
    const waiting = run.review_required_count - run.reviewed_count;
    events.push({
      at: run.created_at,
      kind: "fixes",
      title: published ? "Fixes published" : "Fixes proposed",
      detail: published
        ? `Reviewed facts from ${run.source_label} became the store description agents read. Compiled ${run.created_at.slice(0, 10)}.`
        : waiting > 0
          ? `${String(waiting)} ${waiting === 1 ? "fact waits" : "facts wait"} for your review.`
          : `Facts from ${run.source_label} are reviewed and can be published.`,
      href: `/fixes/${encodeURIComponent(run.run_id)}`,
      status: published
        ? { label: "Published", tone: "ok" }
        : waiting > 0
          ? { label: "Awaiting review", tone: "warn" }
          : { label: "Ready to publish", tone: "info" },
    });
  }

  return events.toSorted((a, b) => b.at.localeCompare(a.at));
}

function launchDetail(launch: EvaluationLaunch): string {
  if (launch.status === "COMPLETED" && launch.missions_completed !== null) {
    return `${String(launch.missions_completed)} of ${String(launch.mission_count)} shopping scenarios executed.`;
  }
  if (launch.status === "QUEUED") {
    return "Waiting to start. Nothing has been executed yet.";
  }
  if (launch.status === "EXECUTING") {
    return launch.missions_completed === null
      ? "Running now."
      : `Running: ${String(launch.missions_completed)} of ${String(launch.mission_count)} scenarios finished.`;
  }
  if (launch.failure_code === "withdrawn_by_merchant") {
    return "Withdrawn before it started. Nothing was measured.";
  }
  if (launch.failure_code === "cancelled_by_operator") {
    return "Closed by your operator before it started. Nothing was measured.";
  }
  return "Did not complete. Open it for the reason.";
}

function launchTone(launch: EvaluationLaunch): MerchantEvent["status"] {
  switch (launch.status) {
    case "COMPLETED":
      return { label: "Completed", tone: "ok" };
    case "EXECUTING":
      return { label: "Running", tone: "info" };
    case "QUEUED":
      return { label: "Queued", tone: "neutral" };
    default:
      if (
        launch.failure_code === "withdrawn_by_merchant" ||
        launch.failure_code === "cancelled_by_operator"
      ) {
        return { label: "Withdrawn", tone: "neutral" };
      }
      return { label: "Not completed", tone: "warn" };
  }
}

/** Simulated captured demand movement for one currency, for the payoff panel. */
export interface CapturedDemandChange {
  readonly currency: string;
  readonly beforeMinor: number;
  readonly afterMinor: number;
}

export interface CompareSummary {
  readonly succeededBefore: number;
  readonly succeededAfter: number;
  readonly purchasesBefore: number;
  readonly purchasesAfter: number;
  /** Missions that newly completed, and missions that no longer complete. */
  readonly improved: number;
  readonly regressed: number;
  readonly capturedDemand: readonly CapturedDemandChange[];
}

/**
 * The numbers the before/after panel leads with, or null when the comparison engine said
 * the two runs cannot be read together. Null is rendered as "Result not interpretable"
 * with the engine's own statement; nothing here ever fabricates a summary past that.
 */
export function compareSummary(comparison: RunComparison): CompareSummary | null {
  if (!comparison.comparable) {
    return null;
  }
  const count = (key: string) => comparison.counts.find((entry) => entry.key === key) ?? null;
  const succeeded = count("missions_succeeded");
  const purchases = count("purchase_missions");
  if (succeeded === null || purchases === null) {
    return null;
  }
  return {
    succeededBefore: succeeded.before,
    succeededAfter: succeeded.after,
    purchasesBefore: purchases.before,
    purchasesAfter: purchases.after,
    improved: comparison.transitions.filter((entry) => entry.direction === "IMPROVED").length,
    regressed: comparison.transitions.filter((entry) => entry.direction === "REGRESSED").length,
    capturedDemand: comparison.simulated_demand
      .filter((change) => change.bucket.toUpperCase() === "CAPTURED")
      .map((change) => ({
        currency: change.currency,
        beforeMinor: change.simulated_before_amount_minor,
        afterMinor: change.simulated_after_amount_minor,
      })),
  };
}
