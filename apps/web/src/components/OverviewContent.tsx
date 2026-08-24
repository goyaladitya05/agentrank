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
              title="No completed benchmark yet"
              explanation="When a benchmark run finishes against your merchant, its health and findings appear here."
            />
          </Panel>
        ) : (
          <Panel>
            {sentences.length === 0 ? (
              <p>No findings on this run. Nothing here needs your action.</p>
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
        <StatusMark
          tone={status.tone}
          label={status.label}
          description={`Run status: ${run.status}`}
        />
        <span className={styles.panelHeadSpacer} />
        <Link className={styles.textLink} href={`/runs/${encodeURIComponent(run.run_id)}`}>
          Open run detail
        </Link>
      </div>
      <KeyValueList
        entries={[
          { term: "Suite", value: run.suite_label },
          { term: "Buyer", value: run.executor_label ?? "not recorded" },
          { term: "Completed", value: formatTimestamp(run.completed_at) },
          {
            term: "Task completion",
            value:
              formatRate(run.task_completion_rate) +
              ` (${String(run.missions_succeeded)} of ${String(run.missions_total)} purchase missions succeeded)`,
          },
          {
            term: "Correct abstentions",
            value:
              formatRate(run.correct_abstention_rate) +
              ` (${String(run.missions_abstained)} abstained)`,
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
          explanation="Your operator can start a benchmark run with the benchmark command line."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll}>
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

function RepresentationPanel({ state }: { state: RepresentationState }) {
  if (state.source_snapshot_id === null && state.compiled_representation_id === null) {
    return (
      <Panel>
        <EmptyState
          title="No compiler representation"
          explanation="Once your merchant source is snapshotted and compiled into an agent-ready representation, its identities appear here."
        />
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
