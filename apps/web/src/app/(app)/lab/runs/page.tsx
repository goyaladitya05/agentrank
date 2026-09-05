import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Section, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { formatMoney, formatRate, formatTimestamp } from "@/lib/format";
import { designationLabel, statusLabel } from "@/lib/labels";
import { decodeRunSummaryList } from "@/lib/insights/decode";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";

export const metadata = { title: "Runs | AgentRank" };

export default async function RunsPage() {
  const outcome = await loadInsight("/api/v1/insights/runs?limit=50", decodeRunSummaryList);
  if (!outcome.ok) {
    return <InsightFailure failure={outcome.failure} />;
  }

  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Benchmark runs</h1>
      </div>
      <Section title="History" hint="Newest first, most recent fifty.">
        <RunsTable runs={outcome.data} />
      </Section>
    </>
  );
}

function RunsTable({ runs }: { runs: readonly import("@/lib/insights/types").RunSummary[] }) {
  if (runs.length === 0) {
    return (
      <div className={styles.panel}>
        <EmptyState
          title="No benchmark runs yet"
          explanation="Every run appears here with its outcomes and designation once one has been executed. An evaluation is what produces one."
        >
          <Link className={styles.textLink} href="/evaluations">
            See what would be evaluated
          </Link>
        </EmptyState>
      </div>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Benchmark runs">
      <table className={`${styles.table} ${styles.tableWide}`}>
        <thead>
          <tr>
            <th scope="col">Started</th>
            <th scope="col">Suite / buyer</th>
            <th scope="col">Designation</th>
            <th scope="col">Status</th>
            <th scope="col" className={styles.num}>
              Succeeded
            </th>
            <th scope="col" className={styles.num}>
              Failed
            </th>
            <th scope="col" className={styles.num}>
              Abstained
            </th>
            <th scope="col" className={styles.num}>
              Completion
            </th>
            <th scope="col">Safety</th>
            <th scope="col" className={styles.num}>
              Provider fails
            </th>
            <th scope="col">Simulated captured demand</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const status = statusLabel(run.status);
            const designation = designationLabel(run.benchmark_designation);
            const safety =
              run.unsafe_completions > 0
                ? { label: `${String(run.unsafe_completions)} escape(s)`, tone: "fail" as const }
                : run.unsafe_attempts > 0
                  ? {
                      label: `${String(run.unsafe_attempts)} blocked`,
                      tone: "ok" as const,
                    }
                  : { label: "No unsafe attempts", tone: "neutral" as const };
            return (
              <tr key={run.run_id}>
                <td>{formatTimestamp(run.started_at)}</td>
                <td>
                  <Link
                    className={styles.rowLinkStrong}
                    href={`/lab/runs/${encodeURIComponent(run.run_id)}`}
                  >
                    {run.suite_label}
                  </Link>
                  <span className={styles.cellMuted}>
                    {run.executor_label !== null ? ` · ${run.executor_label}` : ""}
                  </span>
                </td>
                <td>
                  <StatusMark
                    tone={designation.tone}
                    label={designation.label}
                    description={designation.note}
                  />
                </td>
                <td>
                  <StatusMark tone={status.tone} label={status.label} />
                </td>
                <td className={styles.num}>{String(run.missions_succeeded)}</td>
                <td className={styles.num}>{String(run.missions_failed)}</td>
                <td className={styles.num}>{String(run.missions_abstained)}</td>
                <td className={styles.num}>{formatRate(run.task_completion_rate)}</td>
                <td>
                  <StatusMark tone={safety.tone} label={safety.label} />
                </td>
                <td className={styles.num}>{String(run.provider_failure_missions)}</td>
                <td>
                  {run.simulated_demand.length === 0
                    ? "none recorded"
                    : run.simulated_demand
                        .map((bucket) =>
                          formatMoney(
                            bucket.simulated_captured_demand_amount_minor,
                            bucket.currency,
                          ),
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
