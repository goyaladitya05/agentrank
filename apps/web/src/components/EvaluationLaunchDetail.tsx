import Link from "next/link";

import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import { RunComparisonPanel } from "@/components/RunComparison";
import { TechnicalDetails, IdRow } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import { launchFailureLabel, launchStatusLabel, statusLabel } from "@/lib/labels";
import { REFRESH_SECONDS } from "@/lib/evaluation-refresh";
import type { EvaluationLaunchDetail } from "@/lib/evaluation";

/**
 * One re-evaluation: what it froze, where it has got to, and what it can be read against.
 *
 * Execution state is only what AgentRank actually knows. A queued launch says nothing has run,
 * a running one says how many missions have finished out of how many the suite holds, and
 * neither invents a percentage or a finish time. A launch that did not complete says why in
 * words rather than showing a code.
 */
export function EvaluationLaunchDetailContent({ launch }: { launch: EvaluationLaunchDetail }) {
  const status = launchStatusLabel(launch.status);
  const pending = launch.status === "QUEUED" || launch.status === "EXECUTING";
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Re-evaluation</h1>
        <StatusMark tone={status.tone} label={status.label} />
      </div>

      <Section title="Execution">
        <Panel>
          <ExecutionState launch={launch} />
          {pending ? (
            <p className={styles.reviewMeta} role="status">
              This page re-reads the launch every {REFRESH_SECONDS} seconds until it finishes.
            </p>
          ) : null}
        </Panel>
      </Section>

      <Section title="What was evaluated" hint="Frozen when you asked, and unchanged since.">
        <Panel>
          <KeyValueList
            entries={[
              { term: "Representation", value: launch.representation_label },
              { term: "Benchmark suite", value: launch.suite_label },
              { term: "Missions", value: String(launch.mission_count) },
              { term: "Benchmark world", value: launch.environment_label },
              { term: "Buyer", value: buyerSentence(launch) },
              { term: "Requested", value: formatTimestamp(launch.requested_at) },
              { term: "Started", value: formatTimestamp(launch.started_at) },
              { term: "Finished", value: formatTimestamp(launch.settled_at) },
            ]}
          />
          <TechnicalDetails summary="Frozen identifiers">
            <IdRow label="Re-evaluation id" value={launch.launch_id} />
            <IdRow label="Representation id" value={launch.representation_id} />
            <IdRow label="Compiler run id" value={launch.compiler_run_id} />
            <IdRow label="Suite id" value={launch.suite_id} />
            <IdRow label="Benchmark run id" value={launch.run_id} />
            <IdRow label="Compared against run" value={launch.baseline_run_id} />
            <IdRow label="Buyer configuration" value={launch.buyer_configuration_digest} />
            <IdRow label="Executor kind" value={launch.executor_kind} />
          </TechnicalDetails>
          <p className={styles.reviewMeta}>
            <Link
              className={styles.rowLink}
              href={`/compiler/runs/${encodeURIComponent(launch.compiler_run_id)}`}
            >
              Review the compiler facts behind this representation
            </Link>
          </p>
        </Panel>
      </Section>

      <Section
        title="Compared with your previous run"
        hint="A before and after over time, not a controlled experiment."
      >
        {launch.comparison === null ? (
          <Panel>
            <EmptyState title="No comparison yet" explanation={comparisonAbsence(launch)} />
          </Panel>
        ) : (
          <RunComparisonPanel comparison={launch.comparison} />
        )}
      </Section>
    </>
  );
}

function ExecutionState({ launch }: { launch: EvaluationLaunchDetail }) {
  if (launch.status === "QUEUED") {
    return (
      <>
        <p>Queued. Nothing has been executed yet and no model quota has been spent.</p>
        <p className={styles.reviewMeta}>
          AgentRank runs benchmarks in an operator process rather than inside this page, so a queued
          launch waits for that process to pick it up.
        </p>
      </>
    );
  }
  if (launch.status === "FAILED") {
    return (
      <>
        <p>
          {launch.failure_code === null
            ? "This launch did not complete."
            : launchFailureLabel(launch.failure_code)}
        </p>
        {launch.run_id === null ? (
          <p className={styles.reviewMeta}>
            No benchmark run was started, so nothing was measured and no previous evidence changed.
          </p>
        ) : (
          <RunLink launch={launch} />
        )}
      </>
    );
  }
  return (
    <>
      <p>
        {launch.status === "EXECUTING" ? "Running" : "Completed"}:{" "}
        {launch.missions_completed === null
          ? "no missions recorded yet"
          : `${String(launch.missions_completed)} of ${String(launch.mission_count)} missions finished`}
        .
      </p>
      <RunLink launch={launch} />
    </>
  );
}

function RunLink({ launch }: { launch: EvaluationLaunchDetail }) {
  if (launch.run_id === null) {
    return null;
  }
  const run = launch.run_status === null ? null : statusLabel(launch.run_status);
  return (
    <p className={styles.reviewMeta}>
      <Link className={styles.rowLink} href={`/runs/${encodeURIComponent(launch.run_id)}`}>
        Open the benchmark run and its findings
      </Link>
      {run === null ? "" : ` (run status ${run.label.toLowerCase()})`}
    </p>
  );
}

function buyerSentence(launch: EvaluationLaunchDetail): string {
  if (launch.buyer_profile === "AI_BUYER") {
    return `${launch.provider ?? "model provider"}, requested model ${launch.requested_model ?? "unrecorded"}`;
  }
  return "AgentRank's deterministic reference buyer, which is not an AI agent and did not read the representation";
}

function comparisonAbsence(launch: EvaluationLaunchDetail): string {
  if (launch.baseline_run_id === null) {
    return "You have no earlier completed run of this suite, so there is nothing to read this against. Your next re-evaluation will be compared with this one.";
  }
  if (launch.run_id === null) {
    return "Nothing has been measured yet, so there is nothing to compare.";
  }
  return "This run has not finished, and a partial run's counts describe part of a workload rather than the whole of one.";
}
