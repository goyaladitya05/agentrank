import Link from "next/link";
import { notFound } from "next/navigation";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import { StartCompilerRun } from "@/components/StartCompilerRun";
import { TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";
import { statusLabel } from "@/lib/labels";
import { decodeSourceSnapshot, originLabel, type SourceSnapshot } from "@/lib/source";
import { startCompilerRun } from "@/lib/source-actions";

export const dynamic = "force-dynamic";
export const metadata = { title: "Source snapshot | AgentRank" };

export default async function SourceSnapshotPage({
  params,
}: {
  params: Promise<{ sourceId: string }>;
}) {
  const { sourceId } = await params;
  const outcome = await loadInsight(
    `/api/v1/sources/${encodeURIComponent(sourceId)}`,
    decodeSourceSnapshot,
  );
  if (!outcome.ok)
    return outcome.failure.reason === "notFound" ? (
      notFound()
    ) : (
      <InsightFailure failure={outcome.failure} />
    );
  const snapshot = outcome.data;
  const summary = snapshot.summary;
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Source snapshot {summary.source_label}</h1>
      </div>
      <Section title="Snapshot">
        <Panel>
          <KeyValueList
            entries={[
              { term: "Supplied", value: originLabel(summary.origin) },
              { term: "Created", value: formatTimestamp(summary.created_at) },
              {
                term: "Current",
                value: summary.is_current ? (
                  <StatusMark tone="ok" label="Current" />
                ) : (
                  "Superseded by newer evidence"
                ),
              },
              {
                term: "Describes",
                value: `${String(summary.product_count)} product(s), ${String(summary.variant_count)} variant(s), ${String(summary.policy_count)} policy text(s)`,
              },
            ]}
          />
          <p className={styles.reviewMeta}>
            This snapshot never changes. Newer evidence becomes a new snapshot beside it.
          </p>
          <TechnicalDetails summary="Snapshot identity">
            <p className={styles.mono}>{summary.content_hash}</p>
            <p className={styles.reviewMeta}>
              Snapshot <span className={styles.mono}>{summary.source_snapshot_id}</span>.
            </p>
          </TechnicalDetails>
        </Panel>
      </Section>
      <Section
        title="Compiler"
        hint="Reading source evidence. It publishes nothing and starts no benchmark."
      >
        <Panel>
          <Compiler snapshot={snapshot} />
        </Panel>
      </Section>
      <Section title="Compiler runs over this snapshot">
        <Runs snapshot={snapshot} />
      </Section>
      <Section
        title="Source evidence"
        hint="Every field a proposed fact can cite, as this snapshot states it."
      >
        <Evidence snapshot={snapshot} />
      </Section>
    </>
  );
}

function Compiler({ snapshot }: { snapshot: SourceSnapshot }) {
  return (
    <>
      <StartCompilerRun
        sourceLabel={snapshot.summary.source_label}
        compilable={snapshot.compilable}
        existingRunId={snapshot.existing_run_id}
        action={startCompilerRun.bind(null, snapshot.summary.source_snapshot_id)}
      />
      {snapshot.compilable ? null : (
        <p className={styles.reviewMeta}>
          <Link className={styles.rowLink} href="/sources/new">
            Supply newer source evidence
          </Link>
        </p>
      )}
    </>
  );
}

function Runs({ snapshot }: { snapshot: SourceSnapshot }) {
  if (snapshot.compiler_runs.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="Not compiled yet"
          explanation="Running the compiler over this snapshot produces the facts you review, and it changes nothing else."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Compiler runs over this snapshot">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Run</th>
            <th scope="col">State</th>
            <th scope="col">Review</th>
            <th scope="col">Published</th>
            <th scope="col">Created</th>
          </tr>
        </thead>
        <tbody>
          {snapshot.compiler_runs.map((run) => {
            const status = statusLabel(run.status);
            return (
              <tr key={run.run_id}>
                <td>
                  <Link
                    className={styles.rowLinkStrong}
                    href={`/compiler/runs/${encodeURIComponent(run.run_id)}`}
                  >
                    Review this run
                  </Link>
                </td>
                <td>
                  <StatusMark tone={status.tone} label={status.label} />
                  {run.error_code === null ? null : (
                    <>
                      <br />
                      <span className={styles.cellMuted}>{run.error_code}</span>
                    </>
                  )}
                </td>
                <td>
                  {String(run.reviewed_count)} of {String(run.review_required_count)} resolved
                </td>
                <td>{run.published_representation_id === null ? "No" : "Yes"}</td>
                <td>{formatTimestamp(run.created_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Evidence({ snapshot }: { snapshot: SourceSnapshot }) {
  if (snapshot.fields.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No addressable evidence"
          explanation="This snapshot carries no text a proposed fact could cite."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Source evidence fields">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Source field</th>
            <th scope="col">What it says</th>
          </tr>
        </thead>
        <tbody>
          {snapshot.fields.map((field) => (
            <tr key={field.field}>
              <td className={styles.mono}>{field.field}</td>
              <td>
                {field.excerpt}
                {field.truncated ? (
                  <>
                    <br />
                    <span className={styles.cellMuted}>Shortened for display.</span>
                  </>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
