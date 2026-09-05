import Link from "next/link";

import { StatusMark } from "@/components/Primitives";
import shared from "@/components/console.module.css";
import styles from "@/components/comparison.module.css";
import { formatMoney, formatRate } from "@/lib/format";
import { scenarioName } from "@/lib/insights/journey";
import { compareSummary, type CompareSummary } from "@/lib/insights/merchant";
import {
  comparisonCountLabel,
  comparisonRateLabel,
  conclusionKindLabel,
  demandBucketLabel,
  failureReasonLabel,
  statusLabel,
  transitionDirectionLabel,
  warningLabel,
} from "@/lib/labels";
import type { CountChange, RunComparison } from "@/lib/evaluation";

/**
 * One run read against the run before it.
 *
 * The before and after are the loudest thing here, because they are what the merchant asked
 * for. What qualifies them is never hidden: the engine's own conclusion and every methodology
 * caveat sit directly under the numbers and are not collapsible, so a reader who takes only
 * the top of this panel away already knows this is a before and after over time rather than
 * a controlled experiment.
 *
 * When the engine says the two runs cannot be read together, no number is drawn at all.
 * That refusal, in the engine's own words, is the whole result.
 *
 * Nothing here computes anything. Every delta, every transition and every caveat is the
 * API's, so the console cannot arrive at a different reading of the same two runs.
 */
export function RunComparisonPanel({ comparison }: { comparison: RunComparison }) {
  const conclusion = conclusionKindLabel(comparison.conclusion.kind);
  if (!comparison.comparable) {
    return (
      <div className={styles.refused}>
        <StatusMark tone={conclusion.tone} label={conclusion.label} />
        <p className={styles.refusedTitle}>No before and after can be read from these two runs.</p>
        <p className={styles.conclusion}>{comparison.conclusion.statement}</p>
        <Caveats comparison={comparison} />
      </div>
    );
  }
  // The payoff numbers, when the engine published the two counts they are made of. Their
  // absence is not a refusal: the conclusion, the caveats and the tables still say everything.
  const summary = compareSummary(comparison);
  const tone =
    summary === null
      ? undefined
      : summary.improved > 0 && summary.regressed === 0
        ? "ok"
        : summary.regressed > 0
          ? "warn"
          : undefined;
  return (
    <div className={styles.payoff}>
      {summary === null ? null : (
        <>
          <div className={styles.numbers}>
            <div>
              <span className={styles.sideLabel}>Before</span>
              <span className={styles.value}>
                {String(summary.succeededBefore)} / {String(summary.purchasesBefore)}
              </span>
              <span className={styles.sub}>purchase scenarios completed</span>
            </div>
            <span className={styles.arrow} aria-hidden="true">
              &rarr;
            </span>
            <div>
              <span className={styles.sideLabel}>After</span>
              <span className={styles.value} data-tone={tone}>
                {String(summary.succeededAfter)} / {String(summary.purchasesAfter)}
              </span>
              <span className={styles.sub}>purchase scenarios completed</span>
            </div>
          </div>
          <p className={styles.sentence}>{changeSentence(summary)}</p>
        </>
      )}
      <MovedScenarios comparison={comparison} />
      <p className={styles.conclusion}>
        <span className={styles.conclusionMark}>
          <StatusMark tone={conclusion.tone} label={conclusion.label} />
        </span>
        {comparison.conclusion.statement}
      </p>
      <Caveats comparison={comparison} />
      <details className={styles.full}>
        <summary>Full comparison</summary>
        <div className={styles.fullBody}>
          <Rates comparison={comparison} />
          <Counts comparison={comparison} />
          <Demand comparison={comparison} />
          <Transitions comparison={comparison} />
          <Interactions comparison={comparison} />
          <Runtime comparison={comparison} />
        </div>
      </details>
    </div>
  );
}

/**
 * What moved, in one sentence, from the engine's own transitions. A count of scenarios that
 * newly completed or no longer complete, and never a percentage or a causal claim.
 */
export function changeSentence(summary: CompareSummary): string {
  const parts: string[] = [];
  if (summary.improved > 0) {
    parts.push(
      summary.improved === 1
        ? "Agents completed 1 more shopping scenario"
        : `Agents completed ${String(summary.improved)} more shopping scenarios`,
    );
  }
  if (summary.regressed > 0) {
    parts.push(
      summary.regressed === 1
        ? `${parts.length === 0 ? "Agents completed 1 fewer shopping scenario" : "1 no longer completes"}`
        : `${parts.length === 0 ? `Agents completed ${String(summary.regressed)} fewer shopping scenarios` : `${String(summary.regressed)} no longer complete`}`,
    );
  }
  if (parts.length === 0) {
    return "No scenario changed its outcome between the two runs.";
  }
  const demand = summary.capturedDemand
    .filter((change) => change.beforeMinor !== change.afterMinor)
    .map(
      (change) =>
        `${formatMoney(change.beforeMinor, change.currency)} to ${formatMoney(change.afterMinor, change.currency)}`,
    );
  const sentence = `${parts.join(", and ")}.`;
  return demand.length === 0
    ? sentence
    : `${sentence} Simulated captured demand moved from ${demand.join(" and ")}.`;
}

/**
 * The scenarios that actually moved, named one per line.
 *
 * The aggregate is two numbers; this is which journeys those numbers are made of, which is
 * what a merchant recognises. Direction comes from the comparison engine, so a regression is
 * drawn exactly as loudly as a recovery and neither is inferred here.
 */
function MovedScenarios({ comparison }: { comparison: RunComparison }) {
  const moved = comparison.transitions.filter(
    (entry) => entry.direction === "IMPROVED" || entry.direction === "REGRESSED",
  );
  if (moved.length === 0) {
    return null;
  }
  return (
    <ul className={styles.moved} aria-label="Scenarios that changed">
      {moved.map((entry) => {
        const recovered = entry.direction === "IMPROVED";
        return (
          <li key={entry.mission_key} className={styles.movedRow} data-direction={entry.direction}>
            <span className={styles.movedKey} title={entry.mission_key}>
              {scenarioName(entry.mission_key)}
            </span>
            <span className={styles.movedFrom}>
              {outcome(entry.before_status, entry.before_primary_failure_reason)}
            </span>
            <span className={styles.movedArrow} aria-hidden="true">
              &rarr;
            </span>
            <span className={styles.movedTo}>
              {recovered
                ? "Completed"
                : outcome(entry.after_status, entry.after_primary_failure_reason)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Every caveat the engine raised, visible, and the two runs it read. */
function Caveats({ comparison }: { comparison: RunComparison }) {
  return (
    <>
      <p className={styles.caveatsLabel}>Read this with care</p>
      <ul className={shared.warningList}>
        {comparison.warnings.map((warning) => (
          <li key={warning.code} className={shared.warningItem}>
            <span className={shared.warningCode}>{warningLabel(warning.code)}</span>
            <span>{warning.message}</span>
          </li>
        ))}
      </ul>
      <p className={styles.runs}>
        Comparing the run{" "}
        <Link
          className={shared.rowLink}
          href={`/lab/runs/${encodeURIComponent(comparison.baseline_run_id)}`}
        >
          before
        </Link>{" "}
        with the run{" "}
        <Link
          className={shared.rowLink}
          href={`/lab/runs/${encodeURIComponent(comparison.candidate_run_id)}`}
        >
          after
        </Link>
        , both in the Lab.
      </p>
    </>
  );
}

function Rates({ comparison }: { comparison: RunComparison }) {
  return (
    <div className={shared.tableScroll} tabIndex={0} aria-label="Rates before and after">
      <table className={shared.table}>
        <thead>
          <tr>
            <th scope="col">Rate</th>
            <th scope="col" className={shared.num}>
              Before
            </th>
            <th scope="col" className={shared.num}>
              After
            </th>
          </tr>
        </thead>
        <tbody>
          {comparison.rates.map((rate) => (
            <tr key={rate.key}>
              <td>{comparisonRateLabel(rate.key)}</td>
              <td className={shared.num}>{formatRate(rate.before)}</td>
              <td className={shared.num}>{formatRate(rate.after)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Counts({ comparison }: { comparison: RunComparison }) {
  const moved = comparison.counts.filter((count) => count.delta !== 0);
  const shown = moved.length > 0 ? moved : comparison.counts.slice(0, 4);
  return (
    <div className={shared.tableScroll} tabIndex={0} aria-label="Counts before and after">
      <table className={shared.table}>
        <caption>
          {moved.length > 0
            ? "Counts that moved between the two runs."
            : "No count moved between the two runs. A sample of them is shown."}
        </caption>
        <thead>
          <tr>
            <th scope="col">Count</th>
            <th scope="col" className={shared.num}>
              Before
            </th>
            <th scope="col" className={shared.num}>
              After
            </th>
            <th scope="col" className={shared.num}>
              Change
            </th>
          </tr>
        </thead>
        <tbody>
          {shown.map((count) => (
            <tr key={count.key}>
              <td>{comparisonCountLabel(count.key)}</td>
              <td className={shared.num}>{String(count.before)}</td>
              <td className={shared.num}>{String(count.after)}</td>
              <td className={shared.num}>{signed(count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function signed(count: CountChange): string {
  return count.delta > 0 ? `+${String(count.delta)}` : String(count.delta);
}

function Demand({ comparison }: { comparison: RunComparison }) {
  if (comparison.simulated_demand.length === 0) {
    return null;
  }
  return (
    <div className={shared.tableScroll} tabIndex={0} aria-label="Simulated demand before and after">
      <table className={shared.table}>
        <caption>
          Simulated benchmark demand, one row per currency and bucket. These are authored values,
          never revenue, and currencies are never added together.
        </caption>
        <thead>
          <tr>
            <th scope="col">Currency</th>
            <th scope="col">Bucket</th>
            <th scope="col" className={shared.num}>
              Simulated before
            </th>
            <th scope="col" className={shared.num}>
              Simulated after
            </th>
          </tr>
        </thead>
        <tbody>
          {comparison.simulated_demand.map((change) => (
            <tr key={`${change.currency}:${change.bucket}`}>
              <td className={shared.mono}>{change.currency}</td>
              <td>{demandBucketLabel(change.bucket)}</td>
              <td className={shared.num}>
                {formatMoney(change.simulated_before_amount_minor, change.currency)}
              </td>
              <td className={shared.num}>
                {formatMoney(change.simulated_after_amount_minor, change.currency)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Transitions({ comparison }: { comparison: RunComparison }) {
  if (comparison.transitions.length === 0) {
    return <p className={shared.reviewMeta}>Every mission ended where it ended before.</p>;
  }
  return (
    <div className={shared.tableScroll} tabIndex={0} aria-label="Missions that ended differently">
      <table className={shared.table}>
        <thead>
          <tr>
            <th scope="col">Mission</th>
            <th scope="col">Before</th>
            <th scope="col">After</th>
            <th scope="col">Direction</th>
          </tr>
        </thead>
        <tbody>
          {comparison.transitions.map((transition) => {
            const direction = transitionDirectionLabel(transition.direction);
            return (
              <tr key={transition.mission_key}>
                <td className={shared.mono}>{transition.mission_key}</td>
                <td>
                  {outcome(transition.before_status, transition.before_primary_failure_reason)}
                </td>
                <td>{outcome(transition.after_status, transition.after_primary_failure_reason)}</td>
                <td>
                  <StatusMark tone={direction.tone} label={direction.label} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function outcome(status: string | null, reason: string | null): string {
  if (status === null) {
    return "not present in this run";
  }
  const label = statusLabel(status).label;
  // The reason is a stored enum and this is the last page of the merchant's journey. Rendering
  // it raw put `AGENT_REASONING_ERROR` in a table cell beside a sentence written for a
  // shopkeeper.
  return reason === null ? label : `${label}: ${failureReasonLabel(reason)}`;
}

function Interactions({ comparison }: { comparison: RunComparison }) {
  const {
    model_invocations: invocations,
    tool_calls: calls,
    baseline_traced: before,
    candidate_traced: after,
    token_usage_complete: tokens,
  } = comparison.interactions;
  if (!before && !after) {
    return (
      <p className={shared.reviewMeta}>
        Neither run recorded a model trace, so there is no interaction cost to compare.
      </p>
    );
  }
  if (!before || !after) {
    return (
      <p className={shared.reviewMeta}>
        Only the {before ? "earlier" : "later"} run recorded a model trace, so interaction cost
        cannot be compared between them.
      </p>
    );
  }
  return (
    <p className={shared.reviewMeta}>
      Interaction cost:{" "}
      {invocations === null
        ? "round trips not recorded"
        : `${String(invocations.before)} to ${String(invocations.after)} provider round trips`}
      {calls === null ? "" : `, ${String(calls.before)} to ${String(calls.after)} tool calls`}.{" "}
      {tokens === false
        ? "Some provider invocations reported no token usage, so token totals are unknown and none is shown."
        : ""}
    </p>
  );
}

function Runtime({ comparison }: { comparison: RunComparison }) {
  const before = comparison.baseline_runtime_seconds;
  const after = comparison.candidate_runtime_seconds;
  if (before === null || after === null) {
    return null;
  }
  return (
    <p className={shared.reviewMeta}>
      Wall clock: {Math.round(before)}s before, {Math.round(after)}s after. Execution time depends
      on provider latency and machine load as much as on anything you changed.
    </p>
  );
}
