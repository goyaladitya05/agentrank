import Link from "next/link";

import { DemandTable } from "@/components/Demand";
import { FindingList } from "@/components/Findings";
import { OutcomeBar } from "@/components/OutcomeBar";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import {
  formatCount,
  formatMoney,
  formatRate,
  formatTimestamp,
  truncateMiddle,
} from "@/lib/format";
import { conclusionKindLabel, designationLabel, statusLabel } from "@/lib/labels";
import {
  attentionSentences,
  attentionSummary,
  providerSentence,
  safetyReading,
} from "@/lib/insights/summary";
import type {
  LatestExperiment,
  MerchantOverview,
  RepresentationState,
  RunSummary,
} from "@/lib/insights/types";

/**
 * What an aborted run is, said before anything derived from it is read.
 *
 * A run that stopped part way is the newest evidence there is and its numbers describe only the
 * missions that executed. Without this, a run halted at mission zero rendered as "Nothing here
 * needs your action" above a completion rate of zero per cent, which is two false statements: it
 * needed action, and nothing was measured to be zero per cent of.
 */
function Incomplete({ run }: { run: RunSummary }) {
  const executed =
    run.missions_succeeded + run.missions_failed + run.missions_abstained + run.missions_errored;
  return (
    <p>
      <StatusMark tone="warn" label="Incomplete" /> This evaluation stopped before it finished.{" "}
      {String(executed)} of {String(run.missions_total)} missions executed, so everything below
      describes those and not your merchant as a whole.
    </p>
  );
}

export function OverviewContent({ data }: { data: MerchantOverview }) {
  const latestFinishedRun =
    data.runs.find((run) => run.status === "COMPLETED" || run.status === "ABORTED") ?? null;
  const attention = attentionSummary(data.top_findings);
  const sentences = attentionSentences(attention);
  const safety = latestFinishedRun
    ? safetyReading(latestFinishedRun.unsafe_attempts, latestFinishedRun.unsafe_completions, 0)
    : null;
  const providerNote = latestFinishedRun
    ? providerSentence(latestFinishedRun.provider_failure_missions)
    : null;

  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Overview</h1>
        <TechnicalDetails summary="Diagnostic engine identity">
          <IdRow label="Engine identity" value={data.engine_identity} />
          <IdRow label="Merchant id" value={data.merchant_id} />
        </TechnicalDetails>
      </div>

      <Section title="Do I need to do something?">
        {latestFinishedRun === null ? (
          <Panel>
            <EmptyState
              title="No evaluations have run yet"
              explanation="AgentRank has not measured this merchant, so there is nothing here to act on. An evaluation runs the benchmark suite against your merchant and produces its findings."
            >
              <Link className={styles.textLink} href="/evaluations">
                See what would be evaluated
              </Link>
            </EmptyState>
          </Panel>
        ) : (
          <Panel>
            {latestFinishedRun.status === "ABORTED" ? <Incomplete run={latestFinishedRun} /> : null}
            {sentences.length === 0 ? (
              <p>
                {latestFinishedRun.status === "ABORTED"
                  ? "No findings came out of the part of this run that executed."
                  : "No findings on this run. Nothing here needs your action."}
              </p>
            ) : (
              sentences.map((sentence) => <p key={sentence}>{sentence}</p>)
            )}
            {safety !== null ? (
              <p style={{ marginTop: 8 }}>
                <StatusMark tone={safety.tone} label="Safety" /> {safety.text}
              </p>
            ) : null}
            {providerNote !== null ? (
              <p style={{ marginTop: 8 }}>
                <StatusMark tone="info" label="Provider" /> {providerNote}
              </p>
            ) : null}
          </Panel>
        )}
      </Section>

      {latestFinishedRun !== null ? (
        <Section title="Latest benchmark health">
          <LatestRunHealth run={latestFinishedRun} findingsRunId={data.top_findings_run_id} />
        </Section>
      ) : null}

      <Section title="Top findings">
        <FindingList findings={data.top_findings} runId={data.top_findings_run_id} />
      </Section>

      <Section title="Simulated demand" hint="Benchmark figures. Not revenue.">
        <DemandTable
          buckets={data.simulated_demand_totals_by_currency}
          caption="Totals across your recent runs, one row per currency."
        />
      </Section>

      <Section
        title="Recent runs"
        actions={
          <Link className={styles.textLink} href="/runs">
            All runs
          </Link>
        }
      >
        <RecentRuns runs={data.runs} />
      </Section>

      <Section title="Representation state">
        <RepresentationPanel state={data.representation_state} />
      </Section>

      <Section title="Latest controlled experiment">
        <ExperimentTeaser experiment={data.latest_experiment} />
      </Section>
    </>
  );
}

function LatestRunHealth({
  run,
  findingsRunId,
}: {
  run: RunSummary;
  findingsRunId: string | null;
}) {
  const status = statusLabel(run.status);
  const designation = designationLabel(run.benchmark_designation);
  const unfinished =
    run.missions_total -
    (run.missions_succeeded + run.missions_failed + run.missions_abstained + run.missions_errored);
  return (
    <Panel>
      <div className={styles.panelHead}>
        <StatusMark tone={status.tone} label={status.label} description={status.label} />
        <StatusMark
          tone={designation.tone}
          label={designation.label}
          description={designation.note}
        />
        <span className={styles.panelHeadSpacer} />
        <Link className={styles.textLink} href={`/runs/${encodeURIComponent(run.run_id)}`}>
          Open run detail
        </Link>
      </div>
      {run.status === "ABORTED" ? (
        <p className={styles.reviewMeta}>
          This run stopped before it finished, so every number below is over the missions that
          executed rather than over the suite.
        </p>
      ) : null}
      <KeyValueList
        entries={[
          { term: "Suite", value: run.suite_label },
          { term: "Buyer", value: run.executor_label ?? "not recorded" },
          {
            term: run.status === "ABORTED" ? "Stopped" : "Completed",
            value: formatTimestamp(run.completed_at),
          },
          // Each rate is paired with the fraction that actually produces it. This used to pair
          // task completion with `missions_total`, which includes the control missions the rate
          // is not over, so the percentage and the fraction beside it disagreed and the run
          // detail page said something different about the same run.
          {
            term: "Task completion",
            value:
              formatRate(run.task_completion_rate) +
              ` (${String(run.missions_succeeded)} of ${String(run.purchase_missions)} purchase missions)`,
          },
          {
            term: "Correct abstentions",
            value:
              formatRate(run.correct_abstention_rate) +
              ` (${String(run.correct_abstentions)} of ${String(run.control_missions)} controls)`,
          },
          { term: "Failures", value: formatCount(run.missions_failed, "failed mission") },
          {
            term: "Safety",
            value:
              run.unsafe_attempts === 0 && run.unsafe_completions === 0
                ? "No unsafe attempts"
                : `${String(run.unsafe_attempts)} unsafe attempt(s), ${String(run.unsafe_completions)} escape(s)`,
          },
          {
            term: "Simulated captured demand",
            value:
              run.simulated_demand.length === 0
                ? "none recorded"
                : run.simulated_demand
                    .map((bucket) =>
                      formatMoney(bucket.simulated_captured_demand_amount_minor, bucket.currency),
                    )
                    .join(" · "),
          },
          {
            term: "Provider failures",
            value:
              run.provider_failure_missions === 0
                ? "None on this run"
                : `${String(run.provider_failure_missions)} mission(s), no merchant action required`,
          },
        ]}
      />
      <OutcomeBar
        counts={{
          succeeded: run.missions_succeeded,
          failed: run.missions_failed,
          abstained: run.missions_abstained,
          errored: run.missions_errored,
          unfinished: Math.max(unfinished, 0),
        }}
      />
      {findingsRunId !== null ? (
        <p className={styles.finePrintTight}>
          Findings below are grouped from this run.{" "}
          <Link className={styles.textLink} href={`/runs/${encodeURIComponent(findingsRunId)}`}>
            See every mission
          </Link>
          .
        </p>
      ) : null}
    </Panel>
  );
}

function RecentRuns({ runs }: { runs: readonly RunSummary[] }) {
  if (runs.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No runs yet"
          explanation="Every benchmark run against your merchant appears here once one has been started."
        >
          <Link className={styles.textLink} href="/evaluations">
            See what would be evaluated
          </Link>
        </EmptyState>
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Recent runs">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Started</th>
            <th scope="col">Suite</th>
            <th scope="col">Status</th>
            <th scope="col">Designation</th>
            <th scope="col" className={styles.num}>
              Missions
            </th>
            <th scope="col" className={styles.num}>
              Completion
            </th>
            <th scope="col" className={styles.num}>
              Failures
            </th>
            <th scope="col" className={styles.num}>
              Provider fails
            </th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const status = statusLabel(run.status);
            const designation = designationLabel(run.benchmark_designation);
            return (
              <tr key={run.run_id}>
                <td>{formatTimestamp(run.started_at)}</td>
                <td>
                  <Link
                    className={styles.rowLinkStrong}
                    href={`/runs/${encodeURIComponent(run.run_id)}`}
                  >
                    {run.suite_label}
                  </Link>
                </td>
                <td>
                  <StatusMark tone={status.tone} label={status.label} />
                </td>
                <td>
                  <StatusMark
                    tone={designation.tone}
                    label={designation.label}
                    description={designation.note}
                  />
                </td>
                <td className={styles.num}>{String(run.missions_total)}</td>
                <td className={styles.num}>{formatRate(run.task_completion_rate)}</td>
                <td className={styles.num}>{String(run.missions_failed)}</td>
                <td className={styles.num}>{String(run.provider_failure_missions)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * What a merchant has compiled, and where to go about it.
 *
 * It says nothing about which evaluation a merchant could run. That is decided by the server
 * from their whole history, and this page holds ten runs, so a panel guessing here would
 * contradict the rule for a merchant whose only completed run had scrolled off the list. The
 * evaluation page answers that question, and two sections above already link to it.
 */
function RepresentationPanel({ state }: { state: RepresentationState }) {
  if (state.source_snapshot_id === null && state.compiled_representation_id === null) {
    return (
      <Panel>
        <EmptyState
          title="No compiler representation"
          explanation="Once your merchant source is snapshotted and compiled into an agent-ready representation, its identities appear here."
        >
          <Link className={styles.textLink} href="/sources/new">
            Add your merchant source
          </Link>
        </EmptyState>
      </Panel>
    );
  }
  return (
    <Panel>
      <KeyValueList
        entries={[
          {
            term: "Source snapshot",
            value:
              state.source_snapshot_label ?? truncateMiddle(state.source_snapshot_id ?? "", 20),
          },
          {
            term: "Compiled representation",
            value:
              state.compiled_representation_label ??
              truncateMiddle(state.compiled_representation_id ?? "", 20),
          },
          {
            term: "Review required",
            value:
              state.review_required_facts === 0
                ? "No facts awaiting review"
                : `${String(state.review_required_facts)} fact(s) awaiting merchant review`,
          },
        ]}
      />
      {state.review_required_facts > 0 ? (
        <p className={styles.finePrintTight}>
          <Link className={styles.textLink} href="/compiler">
            Review {String(state.review_required_facts)} semantic fact(s)
          </Link>
          .
        </p>
      ) : null}
      {state.compiled_representation_id === null ? null : (
        <p className={styles.finePrintTight}>
          Publishing a representation never runs a benchmark.{" "}
          <Link className={styles.textLink} href="/evaluations">
            Request a re-evaluation
          </Link>{" "}
          to measure this one.
        </p>
      )}
      <TechnicalDetails summary="Artifact identifiers">
        <IdRow label="Source snapshot id" value={state.source_snapshot_id} />
        <IdRow label="Compiled representation id" value={state.compiled_representation_id} />
      </TechnicalDetails>
    </Panel>
  );
}

function ExperimentTeaser({ experiment }: { experiment: LatestExperiment | null }) {
  if (experiment === null) {
    return (
      <Panel>
        <EmptyState
          title="No controlled experiments yet"
          explanation="A raw versus compiled comparison appears here once an experiment has been created by your operator."
        />
      </Panel>
    );
  }
  const conclusion = conclusionKindLabel(experiment.conclusion_kind);
  const designation = designationLabel(experiment.benchmark_designation);
  return (
    <Panel>
      <div className={styles.panelHead}>
        <StatusMark
          tone={conclusion.tone}
          label={conclusion.label}
          description={`Backend conclusion kind: ${experiment.conclusion_kind}`}
        />
        <StatusMark
          tone={designation.tone}
          label={designation.label}
          description={designation.note}
        />
      </div>
      <blockquote className={styles.quote}>
        &ldquo;{experiment.conclusion_statement}&rdquo;
      </blockquote>
      <KeyValueList
        entries={[
          {
            term: "Completed pairs",
            value: `${String(experiment.completed_sample_pairs)} of the experiment's sample pairs finished`,
          },
        ]}
      />
      {experiment.warnings.map((warning) => (
        <p key={`${warning.code}:${warning.message}`} className={styles.warnLine}>
          <span className={styles.monoMuted}>{warning.code}</span> {warning.message}
        </p>
      ))}
      <p className={styles.finePrintTight}>
        <Link
          className={styles.textLink}
          href={`/experiments/${encodeURIComponent(experiment.experiment_id)}`}
        >
          Open the full comparison
        </Link>
      </p>
    </Panel>
  );
}
