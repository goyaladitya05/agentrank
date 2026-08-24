import Link from "next/link";

import { EmptyState, Panel, Section } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { decodeCompilerOverview } from "@/lib/compiler";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";
import { InsightFailure } from "@/components/InsightFailure";

export const dynamic = "force-dynamic";
export const metadata = { title: "Compiler review | AgentRank" };

export default async function CompilerPage() {
  const outcome = await loadInsight("/api/v1/compiler/overview", decodeCompilerOverview);
  if (!outcome.ok) return <InsightFailure failure={outcome.failure} />;
  const data = outcome.data;
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Compiler review</h1>
      </div>
      <Section title="Current representation">
        <Panel>
          {data.current_representation_id === null
            ? "No published compiler representation."
            : `Published representation: ${data.current_representation_id}`}
        </Panel>
      </Section>
      <Section title="Review queue" hint="Compiler-derived semantic facts only.">
        <Panel>
          {data.review_required_count === 0
            ? "All required reviews are resolved."
            : `${String(data.review_required_count)} semantic fact(s) need review.`}
        </Panel>
      </Section>
      <Section
        title="Compiler runs"
        hint="A settled run never changes. Newer evidence produces a new one."
      >
        <RunTable runs={data.runs} />
      </Section>
      <Section title="Newer source evidence">
        <Panel>
          <p>
            A published representation and the reviews behind it are permanent. To change what
            AgentRank publishes about your catalog, supply newer source evidence and compile it.
          </p>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources">
              Your source history
            </Link>
          </p>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources/new">
              Supply newer source evidence
            </Link>
          </p>
        </Panel>
      </Section>
    </>
  );
}

function RunTable({ runs }: { runs: Awaited<ReturnType<typeof decodeCompilerOverview>>["runs"] }) {
  if (runs.length === 0)
    return (
      <Panel>
        <EmptyState
          title="No compiler runs"
          explanation="When a merchant source is compiled, its review state will appear here."
        />
      </Panel>
    );
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Compiler runs">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Source</th>
            <th scope="col">Status</th>
            <th scope="col">Review</th>
            <th scope="col">Published</th>
            <th scope="col">Created</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td>
                <Link
                  className={styles.rowLinkStrong}
                  href={`/compiler/runs/${encodeURIComponent(run.run_id)}`}
                >
                  {run.source_label}
                </Link>
              </td>
              <td>{run.status}</td>
              <td>
                {String(run.reviewed_count)} of {String(run.review_required_count)} resolved
              </td>
              <td>{run.published_representation_id === null ? "No" : "Yes"}</td>
              <td>{formatTimestamp(run.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
