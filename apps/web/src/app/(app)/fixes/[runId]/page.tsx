import Link from "next/link";
import { notFound } from "next/navigation";

import { CandidateReview } from "@/components/CandidateReview";
import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Panel, Section, StatusMark } from "@/components/Primitives";
import { PublishRepresentation } from "@/components/PublishRepresentation";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import shared from "@/components/console.module.css";
import styles from "@/components/fixes.module.css";
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
 * Each fact is set as what agents can read now, what AgentRank proposes they read instead,
 * and the one sentence of evidence behind the proposal. The merchant approves, corrects or
 * rejects each one; publishing makes the approved set the description agents read. Compiler
 * identities stay behind the technical disclosure.
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
      <Link className={shared.backLink} href="/fixes">
        <span aria-hidden="true">&larr; </span>Fixes
      </Link>
      <div className={shared.pageHeader}>
        <div>
          <h1 className={shared.pageTitle}>Review fixes</h1>
          <p className={shared.pageIntro}>
            {waiting === 0
              ? `Every fact in this batch, from ${run.source_label}, is decided.`
              : `${String(waiting)} of ${String(run.candidates.length)} facts from ${run.source_label} wait for your decision. Each becomes agent-readable only if you approve it.`}
          </p>
        </div>
        <TechnicalDetails summary="Technical details">
          <IdRow label="Compiler run id" value={run.run_id} />
          <IdRow label="Source snapshot id" value={run.source_snapshot_id} />
          <IdRow label="Configuration digest" value={run.configuration_digest} />
          <p className={shared.reviewMeta}>Compiled {formatTimestamp(run.completed_at)}.</p>
        </TechnicalDetails>
      </div>

      <Facts run={run} />

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
        <p className={shared.buttonRow}>
          <Link className={shared.primaryButton} href="/evaluations">
            Measure again
            <span aria-hidden="true"> &rarr;</span>
          </Link>
        </p>
        <p className={shared.finePrint}>
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
      <ul className={shared.launchTerms}>
        {run.readiness.blockers.map((blocker) => (
          <li key={blocker}>{blocker}</li>
        ))}
      </ul>
      {run.status === "COMPLETED" ? null : (
        <p className={shared.reviewMeta}>
          A batch that did not complete cannot be retried: the same store information read the same
          way is the same batch.{" "}
          <Link className={shared.rowLink} href="/sources/new">
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

/** Whether a candidate still waits for the merchant. */
function isPending(candidate: CompilerCandidate): boolean {
  return candidate.state === "REVIEW_REQUIRED" && candidate.review === null;
}

/** A compatibility claim as a merchant reads it, rather than as its four-state token. */
const COMPATIBILITY: Record<string, string> = {
  TRUE: "Compatible",
  FALSE: "Not compatible",
  UNKNOWN: "Not known",
  NOT_APPLICABLE: "Does not apply",
};

/** The proposed value, set as the thing agents would read. */
function proposedText(candidate: CompilerCandidate): string {
  return valueText(candidate, candidate.proposed_value);
}

function valueText(candidate: CompilerCandidate, raw: unknown): string {
  if (candidate.target.includes(".compatibility.") && typeof raw === "string") {
    return COMPATIBILITY[raw] ?? raw;
  }
  const value = renderValue(raw);
  return candidate.unit === null ? value : `${value} ${candidate.unit}`;
}

/**
 * The value a decided fact carries: the merchant's correction where they made one, and the
 * compiler's proposal otherwise. A corrected fact listed under its placeholder would show the
 * one value the merchant explicitly replaced.
 */
function settledText(candidate: CompilerCandidate): string {
  const review = candidate.review;
  if (review !== null && review.decision === "CORRECT" && review.correction !== null) {
    const fact = review.correction.fact;
    if (typeof fact === "object" && fact !== null && "value" in fact) {
      return valueText(candidate, (fact as { value?: unknown }).value);
    }
  }
  return proposedText(candidate);
}

/**
 * A source field address as a merchant reads it.
 *
 * `products[VE-CHG-100].description` is the compiler's address for the description of one
 * product. The address stays in the evidence disclosure; this is how the reason sentence
 * names it.
 */
function describeField(field: string): string {
  const variant = /^products\[([^\]]+)\]\.variants\[([^\]]+)\]\.(\w+)$/.exec(field);
  if (variant !== null) {
    return `the ${variant[3] ?? "field"} of variant ${variant[2] ?? ""}`;
  }
  const product = /^products\[([^\]]+)\]\.(\w+)$/.exec(field);
  if (product !== null) {
    return `the ${product[2] ?? "field"} of ${product[1] ?? ""}`;
  }
  const policy = /^policy_text\.(\w+)$/.exec(field);
  if (policy !== null) {
    return `your ${policy[1] ?? ""} policy text`;
  }
  return field;
}

/** Why AgentRank proposes this fact: the source it read, quoted. */
function Reason({ candidate }: { candidate: CompilerCandidate }) {
  const evidence = candidate.evidence[0];
  if (evidence === undefined) {
    return <p className={styles.why}>The compiler recorded no source excerpt for this fact.</p>;
  }
  const quoted = evidence.excerpt === null ? null : <q>{evidence.excerpt}</q>;
  if (candidate.requires_correction) {
    return (
      <p className={styles.why}>
        Your source states more than one value for this. {describeField(evidence.field)} says{" "}
        {quoted ?? "something AgentRank could not quote"}. AgentRank will not choose between them;
        the correct one is your call.
      </p>
    );
  }
  return (
    <p className={styles.why}>
      Read from {describeField(evidence.field)}
      {quoted === null ? "" : <>, which says {quoted}</>}. Publishing it gives shopping agents a
      structured value they can rely on instead of a sentence they have to interpret.
    </p>
  );
}

function Facts({ run }: { run: CompilerRun }) {
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
  const pending = run.candidates.filter(isPending);
  const settled = run.candidates.filter((candidate) => !isPending(candidate));
  return (
    <>
      {pending.length > 0 ? (
        <Section title={pending.length === 1 ? "Your decision" : "Your decisions"}>
          <ol className={styles.list}>
            {pending.map((candidate) => (
              <li key={candidate.candidate_id}>
                <PendingFact candidate={candidate} run={run} published={published} />
              </li>
            ))}
          </ol>
        </Section>
      ) : null}
      {settled.length > 0 ? (
        <Section
          title="Decided"
          hint="Facts AgentRank read directly from your source, and the ones you have answered."
        >
          <ol className={styles.settled}>
            {settled.map((candidate) => (
              <li key={candidate.candidate_id}>
                <SettledFact candidate={candidate} run={run} />
              </li>
            ))}
          </ol>
        </Section>
      ) : null}
    </>
  );
}

/**
 * One fact still waiting: what agents read now, what AgentRank proposes, why, and the decision.
 * Addressable by identifier, so a diagnostic finding that names this exact candidate can link
 * straight to the entry rather than to the page.
 */
function PendingFact({
  candidate,
  run,
  published,
}: {
  candidate: CompilerCandidate;
  run: CompilerRun;
  published: boolean;
}) {
  return (
    <article
      id={candidate.candidate_id}
      className={styles.fact}
      aria-label={`Fix ${candidate.target}`}
    >
      <div className={styles.factHead}>
        <h3 className={styles.factSubject}>
          {subjectOf(candidate)}
          <span className={styles.factAttribute}>{candidate.attribute}</span>
        </h3>
        <span className={styles.factConfidence}>
          {CONFIDENCE[candidate.confidence] ?? candidate.confidence}
        </span>
      </div>
      <div className={styles.factBody}>
        <div>
          <p className={styles.changeLabel}>Before</p>
          <p className={styles.before}>
            {candidate.requires_correction ? "Two values in your source" : "Not agent-readable"}
          </p>
          <p className={styles.note}>
            {candidate.requires_correction
              ? "Shopping agents have no single value to rely on."
              : published
                ? "Shopping agents had no structured value for this before you published."
                : "Shopping agents have no structured value for this until you publish one."}
          </p>
          <p className={styles.arrow} aria-hidden="true">
            &darr;
          </p>
          <p className={styles.changeLabel} data-tone="accent">
            Proposed
          </p>
          <p className={styles.proposed}>
            {candidate.requires_correction ? "Your correct value" : proposedText(candidate)}
          </p>
          {candidate.requires_correction ? null : (
            <p className={styles.note}>
              {candidate.attribute_kind === null
                ? "Published as a structured fact."
                : `Published as a structured ${candidate.attribute_kind.toLowerCase()}${candidate.unit === null ? "" : ` in ${candidate.unit}`}.`}
            </p>
          )}
        </div>
        <div>
          <p className={styles.whyLabel}>Why AgentRank proposes this</p>
          <Reason candidate={candidate} />
          <CandidateReview
            candidate={candidate}
            action={reviewCandidate.bind(null, run.run_id, candidate.candidate_id)}
          />
          <Evidence candidate={candidate} />
        </div>
      </div>
    </article>
  );
}

/** One fact already decided, by the compiler or by the merchant, in a single compact row. */
function SettledFact({ candidate, run }: { candidate: CompilerCandidate; run: CompilerRun }) {
  return (
    <article
      id={candidate.candidate_id}
      className={styles.settledRow}
      aria-label={`Fix ${candidate.target}`}
    >
      <h3 className={styles.settledSubject}>
        {subjectOf(candidate)}
        <span className={styles.factAttribute}>{candidate.attribute}</span>
      </h3>
      <p className={styles.settledValue}>{settledText(candidate)}</p>
      <div className={styles.settledDecision}>
        <CandidateReview
          candidate={candidate}
          action={reviewCandidate.bind(null, run.run_id, candidate.candidate_id)}
        />
      </div>
      <Evidence candidate={candidate} compact />
    </article>
  );
}

function Evidence({
  candidate,
  compact = false,
}: {
  candidate: CompilerCandidate;
  compact?: boolean;
}) {
  return (
    <details className={compact ? styles.settledEvidence : shared.tech}>
      <summary className={shared.techSummary}>View evidence</summary>
      <div className={shared.techBody}>
        {candidate.evidence.map((evidence) => (
          <p key={evidence.field} className={shared.reviewMeta}>
            <span className={shared.mono}>{evidence.field}</span>
            <br />
            {evidence.excerpt === null
              ? "Source field recorded without an excerpt."
              : evidence.excerpt}
          </p>
        ))}
        {candidate.review !== null ? (
          <p className={shared.reviewMeta}>
            {candidate.review.decision} recorded by {candidate.review.reviewer} at{" "}
            {formatTimestamp(candidate.review.created_at)}. This review is permanent evidence beside
            the compiler proposal, which is unchanged.
          </p>
        ) : null}
        <TechnicalDetails summary="Compiler proposal document">
          <pre className={shared.tracePayload}>{JSON.stringify(candidate.proposal, null, 2)}</pre>
          {candidate.review?.correction != null ? (
            <pre className={shared.tracePayload}>
              {JSON.stringify(candidate.review.correction, null, 2)}
            </pre>
          ) : null}
        </TechnicalDetails>
      </div>
    </details>
  );
}
