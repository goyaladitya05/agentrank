"use client";

import { useActionState } from "react";

import styles from "@/components/console.module.css";
import type { CompilerCandidate } from "@/lib/compiler";
import { renderValue } from "@/lib/fact-value";
import { IDLE_MUTATION, type CompilerMutationState } from "@/lib/compiler-mutation";

/**
 * One merchant decision about one proposed fact.
 *
 * Split in two on purpose. `CandidateReview` owns the action state; `CandidateReviewForm` is a
 * function of that state and nothing else, so every state a merchant can land in, including a
 * conflict answered by another tab and a correction the API refused, is renderable in a test
 * without driving a browser.
 *
 * Nothing here decides anything. Whether a fact may be accepted, what type its correction must
 * be and whether the run is still open are all answered by the API; this shows the answer and
 * keeps what the merchant typed when the answer is no.
 */

export type ReviewAction = (
  state: CompilerMutationState,
  formData: FormData,
) => CompilerMutationState | Promise<CompilerMutationState>;

export function CandidateReview({
  candidate,
  action,
}: {
  candidate: CompilerCandidate;
  action: ReviewAction;
}) {
  const [state, formAction, pending] = useActionState(action, IDLE_MUTATION);
  return (
    <CandidateReviewForm
      candidate={candidate}
      action={formAction}
      state={state}
      pending={pending}
    />
  );
}

export function CandidateReviewForm({
  candidate,
  action,
  state,
  pending,
}: {
  candidate: CompilerCandidate;
  action: string | ((formData: FormData) => void);
  state: CompilerMutationState;
  pending: boolean;
}) {
  if (candidate.review !== null) {
    return <SettledReview candidate={candidate} review={candidate.review} />;
  }
  if (candidate.state !== "REVIEW_REQUIRED") {
    return <p className={styles.reviewDecision}>Accepted by the compiler</p>;
  }
  return (
    <form action={action} aria-label={`Review ${candidate.target}`}>
      <input type="hidden" name="kind" value={candidate.attribute_kind ?? ""} />
      {candidate.requires_correction ? (
        <>
          <p className={styles.reviewMeta}>
            Your source states more than one value, so this fact needs the correct one.
          </p>
          <CorrectionFields candidate={candidate} state={state} />
          <div className={styles.buttonRow}>
            <SubmitButton decision="correct" pending={pending} label="Confirm correction" />
          </div>
        </>
      ) : (
        <>
          <div className={styles.buttonRow}>
            <SubmitButton decision="accept" pending={pending} label="Accept fact" />
            <SubmitButton decision="reject" pending={pending} label="Reject fact" />
          </div>
          <details open={state.values !== null}>
            <summary className={styles.techSummary}>Correct this fact instead</summary>
            <CorrectionFields candidate={candidate} state={state} />
            <div className={styles.buttonRow}>
              <SubmitButton decision="correct" pending={pending} label="Confirm correction" />
            </div>
          </details>
        </>
      )}
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Saving your decision
        </p>
      ) : null}
      {state.message !== null && !pending ? (
        <p className={styles.mutationAlert} role="alert">
          {state.message}
          {state.stale ? " The state shown here is current." : ""}
        </p>
      ) : null}
    </form>
  );
}

function SubmitButton({
  decision,
  label,
  pending,
}: {
  decision: "accept" | "reject" | "correct";
  label: string;
  pending: boolean;
}) {
  return (
    <button
      className={styles.button}
      type="submit"
      name="decision"
      value={decision}
      disabled={pending}
    >
      {label}
    </button>
  );
}

function SettledReview({
  candidate,
  review,
}: {
  candidate: CompilerCandidate;
  review: NonNullable<CompilerCandidate["review"]>;
}) {
  const decision =
    review.decision === "ACCEPT"
      ? "Accepted by you"
      : review.decision === "REJECT"
        ? "Rejected by you"
        : "Corrected by you";
  return (
    <>
      <p className={styles.reviewDecision}>{decision}</p>
      {review.decision === "CORRECT" ? (
        <p className={styles.reviewMeta}>
          Corrected to {renderValue(correctedValue(review.correction))}
          {candidate.unit === null ? "" : ` ${candidate.unit}`}. The compiler proposal is kept.
        </p>
      ) : null}
    </>
  );
}

function CorrectionFields({
  candidate,
  state,
}: {
  candidate: CompilerCandidate;
  state: CompilerMutationState;
}) {
  const entered = state.values;
  const evidence = candidate.evidence[0];
  return (
    <>
      <label className={styles.field}>
        Corrected value
        {candidate.target.includes(".compatibility.") ? (
          <select name="value" defaultValue={entered?.value ?? "TRUE"}>
            <option value="TRUE">Yes, it is compatible</option>
            <option value="FALSE">No, it is not compatible</option>
            <option value="UNKNOWN">Not known</option>
            <option value="NOT_APPLICABLE">Does not apply</option>
          </select>
        ) : candidate.attribute_kind === "BOOLEAN" ? (
          <select name="value" defaultValue={entered?.value ?? "true"}>
            <option value="true">True</option>
            <option value="false">False</option>
          </select>
        ) : (
          <input
            name="value"
            required
            defaultValue={entered?.value ?? ""}
            inputMode={numeric(candidate) ? "numeric" : "text"}
            type={numeric(candidate) ? "number" : "text"}
          />
        )}
      </label>
      <label className={styles.field}>
        Source field
        <input
          name="provenance_field"
          required
          defaultValue={entered?.provenanceField ?? evidence?.field ?? ""}
        />
      </label>
      <label className={styles.field}>
        Source excerpt
        <input
          name="provenance_excerpt"
          maxLength={500}
          defaultValue={entered?.provenanceExcerpt ?? evidence?.excerpt ?? ""}
        />
      </label>
    </>
  );
}

function numeric(candidate: CompilerCandidate): boolean {
  return candidate.attribute_kind === "INTEGER" || candidate.attribute_kind === "MEASUREMENT";
}

function correctedValue(correction: Record<string, unknown> | null): unknown {
  if (correction === null) return null;
  const fact = correction.fact;
  if (typeof fact !== "object" || fact === null) return null;
  return (fact as { value?: unknown }).value ?? null;
}
