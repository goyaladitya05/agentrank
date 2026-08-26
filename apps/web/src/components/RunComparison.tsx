import Link from "next/link";

import { StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { formatMoney, formatRate } from "@/lib/format";
import { compareSummary } from "@/lib/insights/merchant";
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
 * The caveats come first and are not collapsible. A merchant who reads only the top of this
 * panel should already know that this is a before and after over time rather than a controlled
 * experiment, because that is the sentence that decides how every number under it should be
 * read.
 *
 * Nothing here computes anything. Every delta, every transition and every caveat is the API's,
 * so the console cannot arrive at a different reading of the same two runs.
 */
export function RunComparisonPanel({ comparison }: { comparison: RunComparison }) {
  const conclusion = conclusionKindLabel(comparison.conclusion.kind);
  return (
    <div className={styles.panel}>
      <div className={styles.panelBody}>
        <div className={styles.findingTop}>
          <StatusMark tone={conclusion.tone} label={conclusion.label} />
          <p className={styles.findingTitle}>{comparison.conclusion.statement}</p>
        </div>
        <p className={styles.reviewMeta}>
          Comparing run{" "}
          <Link
            className={styles.rowLink}
            href={`/lab/runs/${encodeURIComponent(comparison.baseline_run_id)}`}
          >
            before
          </Link>{" "}
          with run{" "}
          <Link
            className={styles.rowLink}
            href={`/lab/runs/${encodeURIComponent(comparison.candidate_run_id)}`}
          >
            after
          </Link>
          .
        </p>
        <Warnings comparison={comparison} />
        <MerchantCompare comparison={comparison} />
        {comparison.comparable ? (
          <>
            <Rates comparison={comparison} />
            <Counts comparison={comparison} />
            <Demand comparison={comparison} />
            <Transitions comparison={comparison} />
            <Interactions comparison={comparison} />
            <Runtime comparison={comparison} />
          </>
        ) : null}
      </div>
    </div>
  );
}

/**
 * The payoff, in the terms a merchant asked the question in: how many purchase scenarios
 * completed before and after, what moved, and what the simulated captured demand did.
 *
 * Renders nothing when the comparison engine said the two runs cannot be read together;
 * the conclusion above already says that, and no summary is fabricated past it.
 */
function MerchantCompare({ comparison }: { comparison: RunComparison }) {
  const summary = compareSummary(comparison);
  if (summary === null) {
    return null;
  }
  const changes: { text: string; tone: "ok" | "warn" | "neutral"; delta: string }[] = [];
  if (summary.improved > 0) {
    changes.push({
      delta: `+${String(summary.improved)}`,
      tone: "ok",
      text:
        summary.improved === 1
          ? "previously failed scenario now succeeds"
          : "previously failed scenarios now succeed",
    });
  }
  if (summary.regressed > 0) {
    changes.push({
      delta: `−${String(summary.regressed)}`,
      tone: "warn",
      text:
        summary.regressed === 1 ? "scenario no longer completes" : "scenarios no longer complete",
    });
  }
  for (const demand of summary.capturedDemand) {
    if (demand.beforeMinor !== demand.afterMinor) {
      changes.push({
        delta: `${formatMoney(demand.beforeMinor, demand.currency)} → ${formatMoney(demand.afterMinor, demand.currency)}`,
        tone: demand.afterMinor > demand.beforeMinor ? "ok" : "warn",
        text: "simulated captured demand",
      });
    }
  }
  return (
    <>
      <div className={merchant.compare}>
        <div className={merchant.compareSide}>
          <span className={merchant.compareLabel}>Before</span>
          <span className={merchant.compareValue}>
            {String(summary.succeededBefore)} / {String(summary.purchasesBefore)}{" "}
            <small>purchase scenarios completed</small>
          </span>
        </div>
        <div className={merchant.compareSide}>
          <span className={merchant.compareLabel}>After</span>
          <span className={merchant.compareValue}>
            {String(summary.succeededAfter)} / {String(summary.purchasesAfter)}{" "}
            <small>purchase scenarios completed</small>
          </span>
        </div>
      </div>
      {changes.length === 0 ? (
        <p className={styles.reviewMeta}>No scenario changed its outcome between the two runs.</p>
      ) : (
        <ul className={merchant.compareChanges}>
          {changes.map((change) => (
            <li key={`${change.delta}:${change.text}`}>
              <span className={merchant.compareDelta} data-tone={change.tone}>
                {change.delta}
              </span>
              <span>{change.text}</span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function Warnings({ comparison }: { comparison: RunComparison }) {
  return (
    <ul className={styles.warningList}>
      {comparison.warnings.map((warning) => (
        <li key={warning.code} className={styles.warningItem}>
          <span className={styles.warningCode}>{warningLabel(warning.code)}</span>
          <span>{warning.message}</span>
        </li>
      ))}
    </ul>
  );
}

function Rates({ comparison }: { comparison: RunComparison }) {
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Rates before and after">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Rate</th>
            <th scope="col" className={styles.num}>
              Before
            </th>
            <th scope="col" className={styles.num}>
              After
            </th>
          </tr>
        </thead>
        <tbody>
          {comparison.rates.map((rate) => (
            <tr key={rate.key}>
              <td>{comparisonRateLabel(rate.key)}</td>
              <td className={styles.num}>{formatRate(rate.before)}</td>
              <td className={styles.num}>{formatRate(rate.after)}</td>
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
    <>
      <p className={styles.reviewMeta}>
        {moved.length > 0
          ? "Counts that moved between the two runs."
          : "No count moved between the two runs. A sample of them is shown."}
      </p>
      <div className={styles.tableScroll} tabIndex={0} aria-label="Counts before and after">
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Count</th>
              <th scope="col" className={styles.num}>
                Before
              </th>
              <th scope="col" className={styles.num}>
                After
              </th>
              <th scope="col" className={styles.num}>
                Change
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((count) => (
              <tr key={count.key}>
                <td>{comparisonCountLabel(count.key)}</td>
                <td className={styles.num}>{String(count.before)}</td>
                <td className={styles.num}>{String(count.after)}</td>
                <td className={styles.num}>{signed(count)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
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
    <>
      <p className={styles.reviewMeta}>
        Simulated benchmark demand, one row per currency and bucket. These are authored values,
        never revenue, and currencies are never added together.
      </p>
      <div
        className={styles.tableScroll}
        tabIndex={0}
        aria-label="Simulated demand before and after"
      >
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Currency</th>
              <th scope="col">Bucket</th>
              <th scope="col" className={styles.num}>
                Simulated before
              </th>
              <th scope="col" className={styles.num}>
                Simulated after
              </th>
            </tr>
          </thead>
          <tbody>
            {comparison.simulated_demand.map((change) => (
              <tr key={`${change.currency}:${change.bucket}`}>
                <td className={styles.mono}>{change.currency}</td>
                <td>{demandBucketLabel(change.bucket)}</td>
                <td className={styles.num}>
                  {formatMoney(change.simulated_before_amount_minor, change.currency)}
                </td>
                <td className={styles.num}>
                  {formatMoney(change.simulated_after_amount_minor, change.currency)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Transitions({ comparison }: { comparison: RunComparison }) {
  if (comparison.transitions.length === 0) {
    return <p className={styles.reviewMeta}>Every mission ended where it ended before.</p>;
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Missions that ended differently">
      <table className={styles.table}>
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
                <td className={styles.mono}>{transition.mission_key}</td>
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
      <p className={styles.reviewMeta}>
        Neither run recorded a model trace, so there is no interaction cost to compare.
      </p>
    );
  }
  if (!before || !after) {
    return (
      <p className={styles.reviewMeta}>
        Only the {before ? "earlier" : "later"} run recorded a model trace, so interaction cost
        cannot be compared between them.
      </p>
    );
  }
  return (
    <p className={styles.reviewMeta}>
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
    <p className={styles.reviewMeta}>
      Wall clock: {Math.round(before)}s before, {Math.round(after)}s after. Execution time depends
      on provider latency and machine load as much as on anything you changed.
    </p>
  );
}
