import Link from "next/link";

import { DemandTable } from "@/components/Demand";
import { FindingList } from "@/components/Findings";
import { OutcomeBar } from "@/components/OutcomeBar";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import { formatCount, formatMoney, formatRate, formatTimestamp } from "@/lib/format";
import { demandBucketLabel, designationLabel, ownerLabel, statusLabel } from "@/lib/labels";
import { primaryDiagnosisText, ownerOfPrimary, providerFaultMark } from "@/lib/insights/diagnosis";
import { safetyReading } from "@/lib/insights/summary";
import type { MissionDiagnosis, RunDiagnostics } from "@/lib/insights/types";

export const OUTCOME_FILTERS = ["ALL", "SUCCEEDED", "ABSTAINED", "FAILED", "ERRORED"] as const;
export type OutcomeFilter = (typeof OUTCOME_FILTERS)[number];

export function isOutcomeFilter(value: string | undefined): value is OutcomeFilter {
  return (OUTCOME_FILTERS as readonly string[]).includes(value ?? "");
}

/**
 * One benchmark run in full: identity, summary, findings, and its missions.
 *
 * The designation travels everywhere this run's numbers appear, so development results
 * can never quietly stand in for evaluation evidence.
 */
export function RunDetailContent({
  data,
  filter,
}: {
  data: RunDiagnostics;
  filter: OutcomeFilter;
}) {
  const status = statusLabel(data.status);
  const designation = designationLabel(data.benchmark_designation);
  const missions =
    filter === "ALL" ? data.missions : data.missions.filter((mission) => mission.status === filter);
  const unfinished =
    data.metrics.missions_total -
    (data.metrics.missions_succeeded +
      data.metrics.missions_failed +
      data.metrics.missions_abstained +
      data.metrics.missions_errored);

  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Run detail</h1>
        <StatusMark
          tone={designation.tone}
          label={designation.label}
          description={designation.note}
        />
        <StatusMark tone={status.tone} label={status.label} />
      </div>

      <Section title="Run identity">
        <Panel>
          <KeyValueList
            entries={[
              { term: "Suite", value: data.suite_label },
              { term: "Environment", value: data.environment_label ?? "not recorded" },
              {
                term: "Buyer",
                value:
                  data.executor_label === null
                    ? "not recorded"
                    : `${data.executor_label}${
                        data.agent_implementation_version !== null
                          ? ` (implementation v${String(data.agent_implementation_version)})`
                          : ""
                      }`,
              },
              { term: "Created", value: formatTimestamp(data.created_at) },
              { term: "Started", value: formatTimestamp(data.started_at) },
              { term: "Completed", value: formatTimestamp(data.completed_at) },
              {
                term: "Catalog pin",
                value:
                  data.catalog_pin_verified === null
                    ? "no pin recorded"
                    : data.catalog_pin_verified
                      ? "matches the pinned catalog"
                      : "does not match the pinned catalog",
              },
            ]}
          />
          <TechnicalDetails summary="Run identifiers and pins">
            <IdRow label="Run id" value={data.run_id} />
            <IdRow label="Engine identity" value={data.engine_identity} />
            <IdRow label="Representation id" value={data.representation_id} />
            <IdRow label="Representation label" value={data.representation_label} />
            <IdRow label="Compiler run id" value={data.compiler_run_id} />
            <IdRow label="Catalog hash" value={data.catalog_hash} />
            <IdRow label="Evaluator version" value={data.evaluator_version} />
            <IdRow label="Executor revision" value={data.executor_revision} />
          </TechnicalDetails>
        </Panel>
      </Section>

      <Section title="Summary">
        <Panel>
          <KeyValueList
            entries={[
              {
                term: "Task completion",
                value:
                  formatRate(data.metrics.task_completion_rate) +
                  ` (${String(data.metrics.missions_succeeded)} of ${String(
                    data.metrics.purchase_missions,
                  )} purchase missions)`,
              },
              {
                term: "Correct abstentions",
                value:
                  formatRate(data.metrics.correct_abstention_rate) +
                  ` (${String(data.metrics.correct_abstentions)} of ${String(
                    data.metrics.control_missions,
                  )} controls)`,
              },
              {
                term: "Failures",
                value: formatCount(data.metrics.missions_failed, "failed mission"),
              },
              {
                term: "Errored",
                value:
                  data.metrics.missions_errored === 0
                    ? "None"
                    : `${String(data.metrics.missions_errored)} mission(s) the harness could not measure`,
              },
              {
                term: "Unfinished",
                value:
                  unfinished <= 0
                    ? "None"
                    : `${String(unfinished)} mission(s) never reached an outcome`,
              },
              {
                term: "Safety",
                value: safetyText(data),
              },
            ]}
          />
          <OutcomeBar
            counts={{
              succeeded: data.metrics.missions_succeeded,
              failed: data.metrics.missions_failed,
              abstained: data.metrics.missions_abstained,
              errored: data.metrics.missions_errored,
              unfinished: Math.max(unfinished, 0),
            }}
          />
        </Panel>
      </Section>

      <Section title="Simulated demand" hint="Benchmark figures. Not revenue.">
        <DemandTable buckets={data.simulated_demand} caption="This run, one row per currency." />
      </Section>

      <ProviderHealth data={data} />

      <Section title="Findings">
        <FindingList findings={data.findings} runId={data.run_id} />
      </Section>

      <Section title="Missions" actions={<OutcomeFilterNav runId={data.run_id} current={filter} />}>
        <MissionsTable
          missions={missions}
          runId={data.run_id}
          total={data.missions.length}
          filter={filter}
        />
      </Section>
    </>
  );
}

function safetyText(data: RunDiagnostics): string {
  const reading = safetyReading(
    data.metrics.unsafe_attempts,
    data.metrics.unsafe_completions,
    data.metrics.unverified_attempts,
  );
  if (reading !== null) {
    return reading.text;
  }
  return "No unsafe attempts.";
}

function ProviderHealth({ data }: { data: RunDiagnostics }) {
  const health = data.provider_health;
  if (
    health.missions_with_provider_errors === 0 &&
    health.terminated_outages === 0 &&
    health.recovered_throttles === 0
  ) {
    return (
      <Section title="Provider and system health">
        <Panel>
          <p>No model provider problems were recorded on this run.</p>
          {health.requested_model !== null || health.resolved_models.length > 0 ? (
            <KeyValueList
              entries={[
                { term: "Requested model", value: health.requested_model ?? "not recorded" },
                {
                  term: "Resolved models",
                  value:
                    health.resolved_models.length === 0
                      ? "not recorded"
                      : health.resolved_models.join(", "),
                },
              ]}
            />
          ) : null}
        </Panel>
      </Section>
    );
  }
  return (
    <Section title="Provider and system health">
      <Panel>
        <p>
          {health.missions_with_provider_errors > 0
            ? `${String(health.missions_with_provider_errors)} mission(s) saw model provider errors. `
            : ""}
          {health.terminated_outages > 0
            ? `${String(health.terminated_outages)} mission(s) ended on a provider outage. No merchant action is required.`
            : ""}
        </p>
        {health.recovered_throttles > 0 ? (
          <p className={styles.finePrintTight}>
            {String(health.recovered_throttles)} throttled invocation(s) recovered within their
            missions. Operational history only.
          </p>
        ) : null}
        <KeyValueList
          entries={[
            { term: "Requested model", value: health.requested_model ?? "not recorded" },
            {
              term: "Resolved models",
              value:
                health.resolved_models.length === 0
                  ? "not recorded"
                  : health.resolved_models.join(", "),
            },
          ]}
        />
      </Panel>
    </Section>
  );
}

function OutcomeFilterNav({ runId, current }: { runId: string; current: OutcomeFilter }) {
  return (
    <span style={{ display: "inline-flex", gap: 10 }}>
      {OUTCOME_FILTERS.map((outcome) => (
        <Link
          key={outcome}
          href={`/lab/runs/${encodeURIComponent(runId)}${
            outcome === "ALL" ? "" : `?outcome=${outcome}`
          }`}
          className={styles.textLink}
          aria-current={outcome === current ? "true" : undefined}
          style={outcome === current ? { textDecoration: "underline", fontWeight: 600 } : undefined}
        >
          {outcome === "ALL" ? "All" : outcome.charAt(0) + outcome.slice(1).toLowerCase()}
        </Link>
      ))}
    </span>
  );
}

function MissionsTable({
  missions,
  runId,
  total,
  filter,
}: {
  missions: readonly MissionDiagnosis[];
  runId: string;
  total: number;
  filter: OutcomeFilter;
}) {
  if (total === 0) {
    return (
      <div className={styles.panel}>
        <EmptyState
          title="No missions in this run"
          explanation="A run records one mission per suite mission at creation, so an empty list would mean the suite itself had none."
        />
      </div>
    );
  }
  if (missions.length === 0) {
    return (
      <div className={styles.panel}>
        <EmptyState
          title={`No ${currentFilterLabel(filter)} missions`}
          explanation="Change the outcome filter above to see this run's other missions."
        />
      </div>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Missions in this run">
      <table className={styles.table}>
        <caption className={styles.cellMuted} style={{ textAlign: "left", padding: "0 12px 6px" }}>
          {`${missions.length} of ${total} mission(s)`}
        </caption>
        <thead>
          <tr>
            <th scope="col">Mission</th>
            <th scope="col">Outcome</th>
            <th scope="col">Primary diagnosis</th>
            <th scope="col">Owner</th>
            <th scope="col">Provider</th>
            <th scope="col">Simulated demand</th>
          </tr>
        </thead>
        <tbody>
          {missions.map((mission) => {
            const status = statusLabel(mission.status);
            const owner = ownerOfPrimary(mission);
            const fault = providerFaultMark(mission);
            return (
              <tr key={mission.mission_run_id}>
                <td>
                  <Link
                    className={styles.rowLinkStrong}
                    href={`/lab/runs/${encodeURIComponent(runId)}/missions/${encodeURIComponent(
                      mission.mission_run_id,
                    )}`}
                  >
                    <span className={styles.mono}>{mission.mission_key}</span>
                  </Link>
                </td>
                <td>
                  <StatusMark tone={status.tone} label={status.label} />
                </td>
                <td>{primaryDiagnosisText(mission)}</td>
                <td>
                  {owner === null ? (
                    <span className={styles.cellMuted}>none</span>
                  ) : (
                    ownerLabel(owner)
                  )}
                </td>
                <td>
                  {fault === null ? (
                    <span className={styles.cellMuted}>-</span>
                  ) : fault === "Provider outage" ? (
                    <StatusMark tone="warn" label={fault} />
                  ) : (
                    <span className={styles.cellMuted}>{fault}</span>
                  )}
                </td>
                <td>
                  {mission.simulated_demand.length === 0
                    ? "-"
                    : mission.simulated_demand
                        .map(
                          (effect) =>
                            `${demandBucketLabel(effect.bucket)}: ${formatMoney(
                              effect.simulated_amount_minor,
                              effect.currency,
                            )}`,
                        )
                        .join(" · ")}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function currentFilterLabel(status: string): string {
  return status.charAt(0) + status.slice(1).toLowerCase();
}
