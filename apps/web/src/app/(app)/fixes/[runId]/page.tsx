import Link from "next/link";
import { notFound } from "next/navigation";

import { CandidateReview } from "@/components/CandidateReview";
import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Panel, Section, StatusMark } from "@/components/Primitives";
import { PublishRepresentation } from "@/components/PublishRepresentation";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { publishRun, reviewCandidate } from "@/lib/compiler-actions";
import { decodeCompilerRun, type CompilerCandidate, type CompilerRun } from "@/lib/compiler";
import { renderValue } from "@/lib/fact-value";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";
export const metadata = { title: "Review fixes | AgentRank" };

const CONFIDENCE: Record<string, string> = {
  AUTHORITATIVE: "Copied from your source",
  HIGH: "Read from your source text",
  REVIEW_REQUIRED: "Needs your decision",
};

const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * One batch of proposed fixes, reviewed fact by fact and then published.
 *
 * This is the compiler review workflow presented as what it is to a merchant: AgentRank
 * read your store information and proposes precise facts agents can rely on; you approve,
 * correct or reject each one; publishing makes the approved set the description agents
 * read. Compiler identities stay behind the technical disclosure.
 */
export default async function FixReviewPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  // A malformed identifier is an address for something that does not exist, not a server error.
  if (!UUID_SHAPE.test(runId)) {
    return notFound();
  }
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
        <div>
          <p className={merchant.eyebrow}>
            <Link className={merchant.secondaryAction} href="/fixes">
              Fixes
            </Link>{" "}
            / {run.source_label}
          </p>
          <h1 className={styles.pageTitle}>Review fixes</h1>
          <p className={merchant.pageIntro}>
            {waiting === 0
              ? "Every fact in this batch is decided."
              : `${String(waiting)} of ${String(run.candidates.length)} facts wait for your decision. Each becomes agent-readable only if you approve it.`}
          </p>
        </div>
        <TechnicalDetails summary="Technical details">
          <IdRow label="Compiler run id" value={run.run_id} />
          <IdRow label="Source snapshot id" value={run.source_snapshot_id} />
          <IdRow label="Configuration digest" value={run.configuration_digest} />
          <p className={styles.reviewMeta}>Compiled {formatTimestamp(run.completed_at)}.</p>
        </TechnicalDetails>
      </div>

      <Section title="Proposed facts">
        <FactCards run={run} />
      </Section>

      <Section
        title="Publish"
        hint="Publishing never runs an evaluation. Measuring again is a separate step."
      >
        <Panel>
          <Publication run={run} />
        </Panel>
      </Section>
    </>
  );
}

function Publication({ run }: { run: CompilerRun }) {
  if (run.readiness.published_representation_id !== null) {
    return (
      <>
        <p>
          <StatusMark tone="ok" label="Published" /> These fixes are published. Shopping agents
          evaluated against your published description read them.
        </p>
        <p className={styles.finePrintTight}>
          <Link className={merchant.primaryButton} href="/evaluations">
            Measure again
          </Link>
        </p>
        <p className={styles.finePrint}>
          A published batch and the reviews behind it never change. To publish something different,
          supply newer store information and review the facts it produces.
        </p>
        <TechnicalDetails summary="Technical details">
          <IdRow
            label="Published representation id"
            value={run.readiness.published_representation_id}
          />
        </TechnicalDetails>
      </>
    );
  }
  if (run.readiness.publishable) {
    return (
      <PublishRepresentation
        sourceLabel={run.source_label}
        action={publishRun.bind(null, run.run_id)}
      />
    );
  }
  return (
    <>
      <p>These fixes cannot be published yet.</p>
      <ul className={styles.launchTerms}>
        {run.readiness.blockers.map((blocker) => (
          <li key={blocker}>{blocker}</li>
        ))}
      </ul>
      {run.status === "COMPLETED" ? null : (
        <p className={styles.reviewMeta}>
          A batch that did not complete cannot be retried: the same store information read the same
          way is the same batch.{" "}
          <Link className={styles.rowLink} href="/sources/new">
            Supply newer store information
          </Link>{" "}
          to produce a new one.
        </p>
      )}
    </>
  );
}

/** The subject line a merchant reads: the SKU or product, without the target grammar. */
function subjectOf(candidate: CompilerCandidate): string {
  for (const prefix of ["variant.", "product.", "policy."]) {
    if (candidate.product_or_variant.startsWith(prefix)) {
      const name = candidate.product_or_variant.slice(prefix.length);
      return name === candidate.attribute ? "Store policy" : name;
    }
  }
  return candidate.product_or_variant;
}

/** Pending decisions first, then everything already settled. Stable within each group. */
function orderedCandidates(run: CompilerRun): readonly CompilerCandidate[] {
  const pending = run.candidates.filter(
    (candidate) => candidate.state === "REVIEW_REQUIRED" && candidate.review === null,
  );
  const settled = run.candidates.filter(
    (candidate) => !(candidate.state === "REVIEW_REQUIRED" && candidate.review === null),
  );
  return [...pending, ...settled];
}

function FactCards({ run }: { run: CompilerRun }) {
  const published = run.readiness.published_representation_id !== null;
  if (run.candidates.length === 0)
    return (
      <Panel>
        <EmptyState
          title="No facts in this batch"
          explanation="Reading this store information produced nothing reviewable."
        />
      </Panel>
    );
  return (
    <div className={merchant.entryList}>
      {orderedCandidates(run).map((candidate) => (
        // Addressable by identifier, so a diagnostic finding that names this exact
        // candidate can link straight to the entry rather than to the page.
        <article
          key={candidate.candidate_id}
          id={candidate.candidate_id}
          className={merchant.factEntry}
          aria-label={`Fix ${candidate.target}`}
        >
          <div className={merchant.factHead}>
            <h3 className={merchant.factSubject}>
              {subjectOf(candidate)}{" "}
              <span className={merchant.factAttribute}>{candidate.attribute}</span>
            </h3>
            <span className={merchant.factConfidence}>
              {CONFIDENCE[candidate.confidence] ?? candidate.confidence}
            </span>
          </div>
          <div className={merchant.factCompare}>
            <div>
              <span className={merchant.factColLabel}>
                {published ? "Before these fixes" : "Current information"}
              </span>
              <p className={merchant.factBeforeValue}>
                {candidate.requires_correction
                  ? "Your source states more than one value. AgentRank will not choose between them; the correct one is your call."
                  : published
                    ? "Not agent-readable. Shopping agents had no structured value for this before you published."
                    : "Not agent-readable. Shopping agents have no structured value for this until you publish one."}
              </p>
            </div>
            <span className={merchant.factArrow} aria-hidden="true">
              &rarr;
            </span>
            <div className={merchant.factProposed}>
              <span className={merchant.factColLabel}>Proposed agent-ready fact</span>
              <span className={merchant.factValue}>
                {renderValue(candidate.proposed_value)}
                {candidate.unit === null ? "" : ` ${candidate.unit}`}
              </span>
              <span className={merchant.factMeta}>
                {candidate.attribute_kind ?? "fact"}
                {candidate.unit === null ? "" : ` in ${candidate.unit}`}
              </span>
            </div>
          </div>
          <Evidence candidate={candidate} />
          <CandidateReview
            candidate={candidate}
            action={reviewCandidate.bind(null, run.run_id, candidate.candidate_id)}
          />
        </article>
      ))}
    </div>
  );
}

function Evidence({ candidate }: { candidate: CompilerCandidate }) {
  return (
    <details>
      <summary className={styles.techSummary}>View evidence</summary>
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
