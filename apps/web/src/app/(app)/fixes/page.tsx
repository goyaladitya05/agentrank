import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Panel, Section, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { decodeCompilerOverview, type CompilerOverview } from "@/lib/compiler";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";
export const metadata = { title: "Fixes | AgentRank" };

type BatchSummary = CompilerOverview["runs"][number];

/**
 * The merchant Fixes page: facts AgentRank found that could make this store easier for
 * shopping agents to understand, and where each batch of them stands.
 *
 * The compiler stays underneath. What a merchant reviews here is a proposed fact with its
 * evidence; run identifiers, configuration digests and the compiler vocabulary live behind
 * technical disclosures and in the Lab.
 */
export default async function FixesPage() {
  const outcome = await loadInsight("/api/v1/compiler/overview", decodeCompilerOverview);
  if (!outcome.ok) return <InsightFailure failure={outcome.failure} />;
  const data = outcome.data;
  return (
    <>
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>Fixes</h1>
          <p className={merchant.pageIntro}>
            Facts AgentRank read from your own store information that could make your products
            easier for shopping agents to understand. You approve every one before it is published.
          </p>
        </div>
      </div>
      <Lead data={data} />
      <Section
        title="Fix batches"
        hint="Each batch comes from one version of your store information."
      >
        <Batches runs={data.runs} />
      </Section>
    </>
  );
}

/** The one state sentence and action this page leads with. */
function Lead({ data }: { data: CompilerOverview }) {
  const waiting = data.runs.find((run) => run.reviewed_count < run.review_required_count) ?? null;
  if (waiting !== null) {
    const open = waiting.review_required_count - waiting.reviewed_count;
    return (
      <Section title="Waiting for you">
        <Panel>
          <p>
            AgentRank found {String(open)} {open === 1 ? "fact" : "facts"} you can review, from{" "}
            {waiting.source_label}.
          </p>
          <p className={styles.finePrintTight}>
            Accept, correct or reject each one. Nothing is published until you decide.
          </p>
          <p className={styles.finePrintTight}>
            <Link
              className={merchant.primaryButton}
              href={`/fixes/${encodeURIComponent(waiting.run_id)}`}
            >
              Review {String(open)} {open === 1 ? "fix" : "fixes"}
            </Link>
          </p>
        </Panel>
      </Section>
    );
  }
  const publishable = data.runs.find(
    (run) =>
      run.status === "COMPLETED" &&
      run.published_representation_id === null &&
      run.reviewed_count >= run.review_required_count,
  );
  if (publishable !== undefined) {
    return (
      <Section title="Waiting for you">
        <Panel>
          <p>
            {publishable.review_required_count === 0
              ? `No fact from ${publishable.source_label} needs your decision. The fixes can be published.`
              : `Every fact from ${publishable.source_label} is reviewed. The fixes can be published.`}
          </p>
          <p className={styles.finePrintTight}>
            <Link
              className={merchant.primaryButton}
              href={`/fixes/${encodeURIComponent(publishable.run_id)}`}
            >
              Publish fixes
            </Link>
          </p>
        </Panel>
      </Section>
    );
  }
  if (data.current_representation_id !== null) {
    return (
      <Section title="Where you stand">
        <Panel>
          <p>
            Your reviewed fixes are published. Shopping agents evaluated against your published
            description read them.
          </p>
          <p className={styles.finePrintTight}>
            <Link className={merchant.primaryButton} href="/evaluations">
              Measure again
            </Link>
          </p>
          <p className={styles.finePrint}>
            To change what is published, supply newer store information and review the new facts it
            produces.{" "}
            <Link className={styles.rowLink} href="/sources">
              Your store information
            </Link>
            .
          </p>
        </Panel>
      </Section>
    );
  }
  return null;
}

function Batches({ runs }: { runs: readonly BatchSummary[] }) {
  if (runs.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No fixes proposed yet"
          explanation="Fixes come from your store information: AgentRank reads it and proposes precise, agent-readable facts for you to approve. Your store information page is where that starts."
        >
          <Link className={styles.textLink} href="/sources">
            Open your store information
          </Link>
        </EmptyState>
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Fix batches">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">From</th>
            <th scope="col">Facts</th>
            <th scope="col">State</th>
            <th scope="col">Compiled</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td>
                <Link
                  className={styles.rowLinkStrong}
                  href={`/fixes/${encodeURIComponent(run.run_id)}`}
                >
                  {run.source_label}
                </Link>
              </td>
              <td>
                {String(run.reviewed_count)} of {String(run.review_required_count)} reviewed
              </td>
              <td>
                <BatchState run={run} />
              </td>
              <td>{formatTimestamp(run.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BatchState({ run }: { run: BatchSummary }) {
  if (run.published_representation_id !== null) {
    return <StatusMark tone="ok" label="Published" />;
  }
  if (run.status !== "COMPLETED") {
    return <StatusMark tone="warn" label="Did not complete" />;
  }
  if (run.reviewed_count < run.review_required_count) {
    return <StatusMark tone="warn" label="Awaiting review" />;
  }
  return <StatusMark tone="info" label="Ready to publish" />;
}
