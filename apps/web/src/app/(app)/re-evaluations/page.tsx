import { randomUUID } from "node:crypto";

import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { LaunchReevaluation } from "@/components/LaunchReevaluation";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import { launchStatusLabel } from "@/lib/labels";
import { loadInsight } from "@/lib/insights/load";
import { requestReevaluation } from "@/lib/reevaluation-actions";
import {
  decodePreflight,
  decodeReevaluationList,
  type Reevaluation,
  type ReevaluationPreflight,
} from "@/lib/reevaluation";

export const dynamic = "force-dynamic";
export const metadata = { title: "Re-evaluation | AgentRank" };

export default async function ReevaluationsPage() {
  const preflight = await loadInsight(
    "/api/v1/benchmark/re-evaluations/preflight",
    decodePreflight,
  );
  if (!preflight.ok) return <InsightFailure failure={preflight.failure} />;
  const history = await loadInsight("/api/v1/benchmark/re-evaluations?limit=20", (value) =>
    decodeReevaluationList(value),
  );
  if (!history.ok) return <InsightFailure failure={history.failure} />;

  // One rendered form is one launch. The key is generated here, so submitting this form twice
  // or retrying after a lost response is the same request and produces the same launch; opening
  // the page again is a new key and therefore a deliberate second one.
  const requestKey = randomUUID();
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Re-evaluation</h1>
      </div>
      <Section
        title="Request a re-evaluation"
        hint="Publishing a representation never starts a benchmark. This does."
      >
        <Panel>
          <Preflight
            preflight={preflight.data}
            action={requestReevaluation.bind(
              null,
              preflight.data.representation_id ?? "",
              requestKey,
            )}
          />
        </Panel>
      </Section>
      <Section title="Your re-evaluations" hint="Newest first.">
        <History launches={history.data} />
      </Section>
    </>
  );
}

function Preflight({
  preflight,
  action,
}: {
  preflight: ReevaluationPreflight;
  action: Parameters<typeof LaunchReevaluation>[0]["action"];
}) {
  return (
    <>
      <KeyValueList
        entries={[
          {
            term: "Representation under test",
            value: preflight.representation_label ?? "None published",
          },
          { term: "Benchmark suite", value: preflight.suite_label ?? "None published" },
          {
            term: "Missions",
            value: preflight.mission_count === null ? "unknown" : String(preflight.mission_count),
          },
          { term: "Benchmark world", value: preflight.environment_label ?? "Not registered" },
          { term: "Buyer", value: buyerSentence(preflight) },
          {
            term: "Compared against",
            value:
              preflight.baseline_run_id === null
                ? "No earlier completed run of this suite"
                : `Your run completed ${formatTimestamp(preflight.baseline_run_completed_at)}`,
          },
        ]}
      />
      {preflight.launchable && preflight.representation_id !== null ? (
        <LaunchReevaluation preflight={preflight} action={action} />
      ) : (
        <Blockers preflight={preflight} />
      )}
    </>
  );
}

function Blockers({ preflight }: { preflight: ReevaluationPreflight }) {
  return (
    <>
      <p>A re-evaluation cannot be requested right now.</p>
      <ul className={styles.launchTerms}>
        {preflight.blockers.map((blocker) => (
          <li key={blocker.code}>{blocker.message}</li>
        ))}
      </ul>
      {preflight.pending_reevaluation_id === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/re-evaluations/${encodeURIComponent(preflight.pending_reevaluation_id)}`}
          >
            Open the re-evaluation already in progress
          </Link>
        </p>
      )}
    </>
  );
}

function buyerSentence(preflight: ReevaluationPreflight): string {
  if (preflight.buyer_profile === "AI_BUYER") {
    return `${preflight.provider ?? "model provider"}, requested model ${preflight.requested_model ?? "unrecorded"}`;
  }
  return "AgentRank's deterministic reference buyer, which is not an AI agent";
}

function History({ launches }: { launches: readonly Reevaluation[] }) {
  if (launches.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No re-evaluations yet"
          explanation="When you request one, it appears here with what it froze and what became of it."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Re-evaluations">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Requested</th>
            <th scope="col">Representation</th>
            <th scope="col">Suite</th>
            <th scope="col">State</th>
            <th scope="col">Progress</th>
          </tr>
        </thead>
        <tbody>
          {launches.map((launch) => {
            const status = launchStatusLabel(launch.status);
            return (
              <tr key={launch.reevaluation_id}>
                <td>
                  <Link
                    className={styles.rowLinkStrong}
                    href={`/re-evaluations/${encodeURIComponent(launch.reevaluation_id)}`}
                  >
                    {formatTimestamp(launch.requested_at)}
                  </Link>
                </td>
                <td className={styles.mono}>{launch.representation_label}</td>
                <td>{launch.suite_label}</td>
                <td>
                  <StatusMark tone={status.tone} label={status.label} />
                </td>
                <td>
                  {launch.missions_completed === null
                    ? "not started"
                    : `${String(launch.missions_completed)} of ${String(launch.mission_count)} missions`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
