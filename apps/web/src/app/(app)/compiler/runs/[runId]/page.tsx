import Link from "next/link";
import { notFound } from "next/navigation";

import { CandidateReview } from "@/components/CandidateReview";
import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, KeyValueList, Panel, Section } from "@/components/Primitives";
import { PublishRepresentation } from "@/components/PublishRepresentation";
import { TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import { publishRun, reviewCandidate } from "@/lib/compiler-actions";
import { decodeCompilerRun, type CompilerCandidate, type CompilerRun } from "@/lib/compiler";
import { renderValue } from "@/lib/fact-value";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";
export const metadata = { title: "Compiler run review | AgentRank" };

const CONFIDENCE: Record<string, string> = {
  AUTHORITATIVE: "Copied from your source",
  HIGH: "Read from your source text",
  REVIEW_REQUIRED: "Needs your decision",
};

export default async function CompilerRunPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const outcome = await loadInsight(
    `/api/v1/compiler/runs/${encodeURIComponent(runId)}`,
    decodeCompilerRun,
  );
  if (!outcome.ok)
    return outcome.failure.reason === "notFound" ? (
      notFound()
    ) : (
      <InsightFailure failure={outcome.failure} />
    );
  const run = outcome.data;
  const waiting = run.candidates.filter(
    (candidate) => candidate.state === "REVIEW_REQUIRED" && candidate.review === null,
  ).length;
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Review compiler run</h1>
      </div>
      <Section title="Run">
        <Panel>
          <KeyValueList
            entries={[
              {
                term: "Source snapshot",
                value: (
                  <Link
                    className={styles.rowLink}
                    href={`/sources/${encodeURIComponent(run.source_snapshot_id)}`}
                  >
                    {run.source_label}
                  </Link>
                ),
              },
              { term: "Run status", value: run.status },
              {
                term: "Facts awaiting you",
                value:
                  waiting === 0 ? "None" : `${String(waiting)} of ${String(run.candidates.length)}`,
              },
              { term: "Completed", value: formatTimestamp(run.completed_at) },
            ]}
          />
          <TechnicalDetails summary="Compiler identity">
            <p className={styles.mono}>{run.configuration_digest}</p>
            <p className={styles.reviewMeta}>
              Run <span className={styles.mono}>{run.run_id}</span> over source snapshot{" "}
              <span className={styles.mono}>{run.source_snapshot_id}</span>.
            </p>
          </TechnicalDetails>
        </Panel>
      </Section>
      <Section
        title="Publication"
        hint="Publishing never runs a benchmark. Re-evaluation is a separate command."
      >
        <Panel>
          <Publication run={run} />
        </Panel>
      </Section>
      <Section title="Semantic facts">
        <CandidateTable run={run} />
      </Section>
    </>
  );
}

function Publication({ run }: { run: CompilerRun }) {
  if (run.readiness.published_representation_id !== null) {
    return (
      <>
        <p>
          Agent-ready representation published:{" "}
          <span className={styles.mono}>{run.readiness.published_representation_id}</span>
        </p>
        <p className={styles.reviewMeta}>
          This representation and the reviews behind it can no longer change.{" "}
          <Link className={styles.rowLink} href="/sources/new">
            Supply newer source evidence
          </Link>{" "}
          to compile and publish again.
        </p>
        <p className={styles.reviewMeta}>
          Publishing did not run a benchmark. Measuring this representation is a separate command:{" "}
          <Link className={styles.rowLink} href="/re-evaluations">
            request a re-evaluation
          </Link>
          .
        </p>
      </>
    );
  }
  if (run.readiness.publishable) {
    return (
      <PublishRepresentation
        runId={run.run_id}
        sourceLabel={run.source_label}
        action={publishRun.bind(null, run.run_id)}
      />
    );
  }
  return (
    <>
      <p>This run cannot be published yet.</p>
      <ul>
        {run.readiness.blockers.map((blocker) => (
          <li key={blocker}>{blocker}</li>
        ))}
      </ul>
      {run.status === "COMPLETED" ? null : (
        <p className={styles.reviewMeta}>
          A run that did not complete cannot be retried: the same snapshot read the same way is the
          same run.{" "}
          <Link className={styles.rowLink} href="/sources/new">
            Supply newer source evidence
          </Link>{" "}
          to produce a new one.
        </p>
      )}
    </>
  );
}

function CandidateTable({ run }: { run: CompilerRun }) {
  if (run.candidates.length === 0)
    return (
      <Panel>
        <EmptyState
          title="No semantic facts"
          explanation="This compiler run did not produce a reviewable semantic representation."
        />
      </Panel>
    );
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Compiler semantic facts">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Fact</th>
            <th scope="col">What the compiler proposes</th>
            <th scope="col">Evidence and history</th>
            <th scope="col">Your decision</th>
          </tr>
        </thead>
        <tbody>
          {run.candidates.map((candidate) => (
            // Addressable by identifier, so a diagnostic finding that names this exact
            // candidate can link straight to the row rather than to the table.
            <tr key={candidate.candidate_id} id={candidate.candidate_id}>
              <td>
                <strong>{candidate.product_or_variant}</strong>
                <br />
                {candidate.attribute}
                <br />
                <span className={styles.cellMuted}>
                  {candidate.attribute_kind ?? "fact"}
                  {candidate.unit === null ? "" : ` in ${candidate.unit}`}
                </span>
              </td>
              <td>
                <strong>{renderValue(candidate.proposed_value)}</strong>
                {candidate.unit === null ? "" : ` ${candidate.unit}`}
                <br />
                <span className={styles.cellMuted}>
                  {CONFIDENCE[candidate.confidence] ?? candidate.confidence}
                </span>
              </td>
              <td>
                <Evidence candidate={candidate} />
              </td>
              <td>
                <CandidateReview
                  candidate={candidate}
                  action={reviewCandidate.bind(null, run.run_id, candidate.candidate_id)}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Evidence({ candidate }: { candidate: CompilerCandidate }) {
  return (
    <details>
      <summary className={styles.techSummary}>Inspect source evidence</summary>
      {candidate.evidence.map((evidence) => (
        <p key={evidence.field}>
          <span className={styles.mono}>{evidence.field}</span>
          <br />
          {evidence.excerpt === null
            ? "Source field recorded without an excerpt."
            : evidence.excerpt}
        </p>
      ))}
      {candidate.review !== null ? (
        <p className={styles.reviewMeta}>
          {candidate.review.decision} recorded by {candidate.review.reviewer} at{" "}
          {formatTimestamp(candidate.review.created_at)}. This review is permanent evidence beside
          the compiler proposal, which is unchanged.
        </p>
      ) : null}
      <TechnicalDetails summary="Compiler proposal document">
        <pre className={styles.tracePayload}>{JSON.stringify(candidate.proposal, null, 2)}</pre>
        {candidate.review?.correction != null ? (
          <pre className={styles.tracePayload}>
            {JSON.stringify(candidate.review.correction, null, 2)}
          </pre>
        ) : null}
      </TechnicalDetails>
    </details>
  );
}
