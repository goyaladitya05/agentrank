import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";
import {
  decodeSourceOverview,
  originLabel,
  type SourceOverview,
  type SourceSnapshotSummary,
} from "@/lib/source";

export const dynamic = "force-dynamic";
export const metadata = { title: "Source | AgentRank" };

export default async function SourcesPage() {
  const outcome = await loadInsight("/api/v1/sources?limit=20", decodeSourceOverview);
  if (!outcome.ok) return <InsightFailure failure={outcome.failure} />;
  const data = outcome.data;
  const current = data.snapshots.find((snapshot) => snapshot.is_current) ?? null;
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Source</h1>
      </div>
      <Section
        title="Current source snapshot"
        hint="What the compiler reads. Not your live price or stock."
      >
        <Panel>
          <Current current={current} />
        </Panel>
      </Section>
      <Section title="Source history" hint="Newest first. Snapshots never change.">
        <History snapshots={data.snapshots} />
      </Section>
    </>
  );
}

function Current({ current }: { current: SourceSnapshotSummary | null }) {
  if (current === null) {
    return (
      <>
        <p>You have no source snapshot yet.</p>
        <p className={styles.reviewMeta}>
          A source snapshot is the merchant evidence the compiler reads. Supplying one does not
          change any price, stock level or order.
        </p>
        <p className={styles.reviewMeta}>
          <Link className={styles.rowLink} href="/sources/import">
            Import it from your own public pages
          </Link>
        </p>
        <p className={styles.reviewMeta}>
          <Link className={styles.rowLink} href="/sources/new">
            Or write your source document yourself
          </Link>
        </p>
      </>
    );
  }
  return (
    <>
      <KeyValueList
        entries={[
          { term: "Snapshot", value: current.source_label },
          { term: "Supplied", value: originLabel(current.origin) },
          { term: "Created", value: formatTimestamp(current.created_at) },
          {
            term: "Describes",
            value: `${String(current.product_count)} product(s), ${String(current.variant_count)} variant(s), ${String(current.policy_count)} policy text(s)`,
          },
          {
            term: "Compiler runs",
            value:
              current.compiler_run_count === 0
                ? "None yet"
                : `${String(current.compiler_run_count)} over this snapshot`,
          },
        ]}
      />
      <p className={styles.reviewMeta}>
        <Link
          className={styles.rowLink}
          href={`/sources/${encodeURIComponent(current.source_snapshot_id)}`}
        >
          Open this snapshot
        </Link>
      </p>
      <p className={styles.reviewMeta}>
        <Link className={styles.rowLink} href="/sources/import">
          Import newer evidence from your own public pages
        </Link>
      </p>
      <p className={styles.reviewMeta}>
        <Link className={styles.rowLink} href="/sources/new">
          Or supply newer source evidence yourself
        </Link>
      </p>
    </>
  );
}

function History({ snapshots }: { snapshots: SourceOverview["snapshots"] }) {
  if (snapshots.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No source snapshots"
          explanation="When you supply source evidence, every version of it appears here and none of them ever changes."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Source snapshots">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Snapshot</th>
            <th scope="col">Supplied</th>
            <th scope="col">Created</th>
            <th scope="col">Size</th>
            <th scope="col">Compiler runs</th>
            <th scope="col">Published</th>
          </tr>
        </thead>
        <tbody>
          {snapshots.map((snapshot) => (
            <tr key={snapshot.source_snapshot_id}>
              <td>
                <Link
                  className={styles.rowLinkStrong}
                  href={`/sources/${encodeURIComponent(snapshot.source_snapshot_id)}`}
                >
                  {snapshot.source_label}
                </Link>
                {snapshot.is_current ? (
                  <>
                    <br />
                    <StatusMark tone="ok" label="Current" />
                  </>
                ) : null}
              </td>
              <td>{originLabel(snapshot.origin)}</td>
              <td>{formatTimestamp(snapshot.created_at)}</td>
              <td>
                {String(snapshot.product_count)} product(s)
                <br />
                <span className={styles.cellMuted}>
                  {String(snapshot.variant_count)} variant(s)
                </span>
              </td>
              <td>{String(snapshot.compiler_run_count)}</td>
              <td>{String(snapshot.published_representation_count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
