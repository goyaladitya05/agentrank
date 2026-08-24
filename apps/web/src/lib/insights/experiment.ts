/**
 * Reading an experiment's aggregates as comparable rows.
 *
 * The backend computes every number and every warning; this module only lays the two arms
 * side by side and attaches explicit meaning to changes that carry moral weight. An
 * increase in unsafe completions must never read as good news just because it points up,
 * and parity must never be dressed up as success.
 */

import { formatCount, formatRate } from "@/lib/format";
import type { ArmAggregate, ExperimentComparison, RunMetrics } from "@/lib/insights/types";

export function armByLabel(arms: readonly ArmAggregate[], label: string): ArmAggregate | null {
  return arms.find((arm) => arm.arm === label) ?? null;
}

export interface OutcomeDeltaRow {
  readonly metric: string;
  readonly raw: string;
  readonly compiled: string;
  readonly change: string;
  /** Explicit meaning when the direction itself could mislead. Null keeps it neutral. */
  readonly note: string | null;
}

export function outcomeDeltaRows(
  rawTotals: RunMetrics | null,
  compiledTotals: RunMetrics | null,
): OutcomeDeltaRow[] {
  if (rawTotals === null || compiledTotals === null) {
    return [];
  }

  const rate = (metrics: RunMetrics): number | null =>
    metrics.purchase_missions === 0 ? null : metrics.missions_succeeded / metrics.purchase_missions;

  const abstention = (metrics: RunMetrics): number | null =>
    metrics.control_missions === 0 ? null : metrics.correct_abstentions / metrics.control_missions;

  const signed = (delta: number): string => (delta > 0 ? `+${String(delta)}` : String(delta));

  const rows: OutcomeDeltaRow[] = [
    {
      metric: "Purchase missions completed",
      raw: formatRate(rate(rawTotals)),
      compiled: formatRate(rate(compiledTotals)),
      change: signed(compiledTotals.missions_succeeded - rawTotals.missions_succeeded),
      note: null,
    },
    {
      metric: "Correct abstentions",
      raw: formatRate(abstention(rawTotals)),
      compiled: formatRate(abstention(compiledTotals)),
      change: signed(compiledTotals.correct_abstentions - rawTotals.correct_abstentions),
      note: null,
    },
    {
      metric: "Failed missions",
      raw: String(rawTotals.missions_failed),
      compiled: String(compiledTotals.missions_failed),
      change: signed(compiledTotals.missions_failed - rawTotals.missions_failed),
      note: null,
    },
    {
      metric: "Unsafe purchase attempts",
      raw: String(rawTotals.unsafe_attempts),
      compiled: String(compiledTotals.unsafe_attempts),
      change: signed(compiledTotals.unsafe_attempts - rawTotals.unsafe_attempts),
      note:
        compiledTotals.unsafe_attempts > rawTotals.unsafe_attempts
          ? "The compiled arm saw more unsafe attempts. Every one of them being blocked would still mean safety held."
          : null,
    },
    {
      metric: "Safety escapes",
      raw: String(rawTotals.unsafe_completions),
      compiled: String(compiledTotals.unsafe_completions),
      change: signed(compiledTotals.unsafe_completions - rawTotals.unsafe_completions),
      note:
        compiledTotals.unsafe_completions > rawTotals.unsafe_completions
          ? "Escapes increased under the compiled representation. This is worse, whatever the other rows say."
          : null,
    },
    {
      metric: "Denials protecting the buyer",
      raw: formatCount(rawTotals.mandate_denials_protecting, "denial"),
      compiled: formatCount(compiledTotals.mandate_denials_protecting, "denial"),
      change: signed(
        compiledTotals.mandate_denials_protecting - rawTotals.mandate_denials_protecting,
      ),
      note: null,
    },
  ];
  return rows;
}

export function pairOrderLabel(pairOrder: string): string {
  switch (pairOrder) {
    case "counterbalanced":
      return "Counterbalanced (odd pairs raw first, even pairs compiled first)";
    case "raw_then_compiled":
      return "Raw always ran first (historical plan)";
    default:
      return pairOrder;
  }
}

export function conclusionSentence(conclusion: ExperimentComparison["conclusion"]): string {
  return conclusion.statement;
}
