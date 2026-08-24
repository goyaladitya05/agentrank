import { notFound } from "next/navigation";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Panel, Section } from "@/components/Primitives";
import { PublishRepresentation } from "@/components/PublishRepresentation";
import styles from "@/components/console.module.css";
import { publishRun, reviewCandidate } from "@/lib/compiler-actions";
import { decodeCompilerRun } from "@/lib/compiler";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";

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
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Review compiler run</h1>
      </div>
      <Section title="Run">
        <Panel>
          <dl className={styles.keyValueList}>
            <dt>Source snapshot</dt>
            <dd>{run.source_label}</dd>
            <dt>Compiler configuration</dt>
            <dd className={styles.mono}>{run.configuration_digest}</dd>
            <dt>Completed</dt>
            <dd>{formatTimestamp(run.completed_at)}</dd>
          </dl>
        </Panel>
      </Section>
      <Section title="Publication">
        <Panel>
          {run.readiness.published_representation_id !== null ? (
            <p>
              Agent-ready representation published:{" "}
              <span className={styles.mono}>{run.readiness.published_representation_id}</span>
            </p>
          ) : run.readiness.publishable ? (
            <PublishRepresentation
              runId={run.run_id}
              sourceLabel={run.source_label}
              action={publishRun.bind(null, run.run_id)}
            />
          ) : (
            <>
              <p>This run cannot be published yet.</p>
              <ul>
                {run.readiness.blockers.map((blocker) => (
                  <li key={blocker}>{blocker}</li>
                ))}
              </ul>
            </>
          )}
        </Panel>
      </Section>
      <Section title="Semantic facts">
        <CandidateTable run={run} />
      </Section>
    </>
  );
}

function CandidateTable({ run }: { run: Awaited<ReturnType<typeof decodeCompilerRun>> }) {
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
            <th scope="col">Target</th>
            <th scope="col">Proposal</th>
            <th scope="col">Evidence and history</th>
            <th scope="col">Review</th>
          </tr>
        </thead>
        <tbody>
          {run.candidates.map((candidate) => (
            <tr key={candidate.candidate_id}>
              <td>
                <strong>{candidate.product_or_variant}</strong>
                <br />
                {candidate.attribute}
                <br />
                <span className={styles.cellMuted}>
                  {candidate.attribute_kind ?? "fact"}
                  {candidate.unit === null ? "" : ` (${candidate.unit})`}
                </span>
              </td>
              <td>
                <pre className={styles.tracePayload}>
                  {JSON.stringify(candidate.proposal, null, 2)}
                </pre>
              </td>
              <td>
                <details>
                  <summary className={styles.techSummary}>Inspect source evidence</summary>
                  {candidate.evidence.map((evidence) => (
                    <p key={evidence.field}>
                      <span className={styles.mono}>{evidence.field}</span>
                      <br />
                      {evidence.excerpt ?? "Source field recorded without an excerpt."}
                    </p>
                  ))}
                  {candidate.review !== null ? (
                    <p>
                      <strong>{candidate.review.decision}</strong> by {candidate.review.reviewer} at{" "}
                      {formatTimestamp(candidate.review.created_at)}
                      <br />
                      {candidate.review.correction === null ? (
                        "Original compiler proposal retained."
                      ) : (
                        <pre className={styles.tracePayload}>
                          {JSON.stringify(candidate.review.correction, null, 2)}
                        </pre>
                      )}
                    </p>
                  ) : null}
                </details>
              </td>
              <td>
                <ReviewForm runId={run.run_id} candidate={candidate} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReviewForm({
  runId,
  candidate,
}: {
  runId: string;
  candidate: Awaited<ReturnType<typeof decodeCompilerRun>>["candidates"][number];
}) {
  if (candidate.review !== null || candidate.state === "ACCEPTED")
    return <span>{candidate.state}</span>;
  const action = reviewCandidate.bind(null, runId, candidate.candidate_id);
  const evidence = candidate.evidence[0];
  return (
    <form action={action}>
      <input type="hidden" name="kind" value={candidate.attribute_kind ?? ""} />
      {candidate.requires_correction ? (
        <>
          <CorrectionFields candidate={candidate} evidence={evidence} />
          <button className={styles.button} type="submit" name="decision" value="correct">
            Confirm correction
          </button>
        </>
      ) : (
        <>
          <button className={styles.button} type="submit" name="decision" value="accept">
            Accept fact
          </button>
          <button className={styles.textLink} type="submit" name="decision" value="reject">
            Reject fact
          </button>
          <details>
            <summary className={styles.techSummary}>Correct this fact</summary>
            <CorrectionFields candidate={candidate} evidence={evidence} />
            <button className={styles.button} type="submit" name="decision" value="correct">
              Confirm correction
            </button>
          </details>
        </>
      )}
    </form>
  );
}

function CorrectionFields({
  candidate,
  evidence,
}: {
  candidate: Awaited<ReturnType<typeof decodeCompilerRun>>["candidates"][number];
  evidence:
    | Awaited<ReturnType<typeof decodeCompilerRun>>["candidates"][number]["evidence"][number]
    | undefined;
}) {
  const compatibility = candidate.target.includes(".compatibility.");
  return (
    <>
      <label>
        Corrected value
        {compatibility ? (
          <select name="value" defaultValue="TRUE">
            <option value="TRUE">True</option>
            <option value="FALSE">False</option>
            <option value="UNKNOWN">Unknown</option>
            <option value="NOT_APPLICABLE">Not applicable</option>
          </select>
        ) : candidate.attribute_kind === "BOOLEAN" ? (
          <select name="value" defaultValue="true">
            <option value="true">True</option>
            <option value="false">False</option>
          </select>
        ) : (
          <input
            name="value"
            required
            type={
              candidate.attribute_kind === "INTEGER" || candidate.attribute_kind === "MEASUREMENT"
                ? "number"
                : "text"
            }
          />
        )}
      </label>
      <label>
        Source field
        <input name="provenance_field" required defaultValue={evidence?.field ?? ""} />
      </label>
      <label>
        Source excerpt
        <input name="provenance_excerpt" maxLength={500} defaultValue={evidence?.excerpt ?? ""} />
      </label>
    </>
  );
}
