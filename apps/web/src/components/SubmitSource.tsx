"use client";

import Link from "next/link";
import { useActionState } from "react";

import styles from "@/components/console.module.css";
import { IDLE_SUBMISSION, type SourceSubmissionState } from "@/lib/source-mutation";

/**
 * Supplying newer source evidence.
 *
 * The editor holds the merchant's own source document in AgentRank's canonical format. That is
 * deliberately what it is rather than a form over invented fields: this document is the exact
 * shape the deterministic compiler reads, every address a proposed fact can cite is an address
 * inside it, and a friendlier surface over a different shape would be a surface whose fields the
 * evidence trail could not name.
 *
 * Nothing here decides anything. What a valid document is, which version it becomes and whether
 * it says anything new are all answered by the API; this shows the answer and keeps what the
 * merchant wrote when the answer is no.
 *
 * Split in two so that every state a merchant can land in, including a refusal, a document that
 * changed nothing and a response nobody saw, is renderable in a test without driving a browser.
 */

export type SubmitSourceAction = (
  state: SourceSubmissionState,
  formData: FormData,
) => SourceSubmissionState | Promise<SourceSubmissionState>;

export function SubmitSource({
  initialDocument,
  hasCurrentSource,
  action,
}: {
  initialDocument: string;
  hasCurrentSource: boolean;
  action: SubmitSourceAction;
}) {
  const [state, formAction, pending] = useActionState(action, IDLE_SUBMISSION);
  if (state.ok) {
    return <SubmissionAccepted state={state} />;
  }
  return (
    <SubmitSourceForm
      initialDocument={initialDocument}
      hasCurrentSource={hasCurrentSource}
      action={formAction}
      state={state}
      pending={pending}
    />
  );
}

/**
 * What a merchant is told when the submission was accepted.
 *
 * The two outcomes read as two different sentences because they are two different facts. A new
 * snapshot is newer evidence to compile. A document identical to the current snapshot wrote
 * nothing, and telling a merchant "source snapshot created" for that would be telling them their
 * edit landed when there was no edit.
 */
export function SubmissionAccepted({ state }: { state: SourceSubmissionState }) {
  return (
    <div role="status">
      <p>
        {state.createdSnapshot
          ? "Source snapshot created. Nothing has been compiled yet."
          : "This document matches your current source snapshot, so no new snapshot was created."}
      </p>
      {state.snapshotId === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/sources/${encodeURIComponent(state.snapshotId)}`}
          >
            {state.createdSnapshot
              ? "Open this source snapshot"
              : "Open your current source snapshot"}
          </Link>
        </p>
      )}
    </div>
  );
}

export function SubmitSourceForm({
  initialDocument,
  hasCurrentSource,
  action,
  state,
  pending,
}: {
  initialDocument: string;
  hasCurrentSource: boolean;
  action: string | ((formData: FormData) => void);
  state: SourceSubmissionState;
  pending: boolean;
}) {
  const value = state.values?.document ?? initialDocument;
  return (
    <form action={action} aria-label="Supply newer source evidence">
      <p>
        {hasCurrentSource
          ? "This is your current source document. Edit it and submit to create a newer snapshot."
          : "You have no source snapshot yet. Enter your source document to create the first one."}
      </p>
      <ul className={styles.launchTerms}>
        <li>Submitting stores an immutable snapshot. Your existing snapshots do not change.</li>
        <li>Nothing is compiled here. Running the compiler is a separate command.</li>
        <li>
          This does not change any price, stock level or order. Your commerce runtime remains the
          only place those are decided.
        </li>
      </ul>
      <label className={styles.field} htmlFor="source-document">
        Source document
      </label>
      <textarea
        id="source-document"
        name="document"
        className={styles.editor}
        rows={20}
        spellCheck={false}
        required
        defaultValue={value}
        aria-describedby={state.message === null ? undefined : "source-document-error"}
      />
      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          Submit source document
        </button>
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Storing your source document
        </p>
      ) : null}
      {state.message !== null && !pending ? (
        <p className={styles.mutationAlert} role="alert" id="source-document-error">
          {state.message}
          {state.stale && !state.unknown ? " The state shown here is current." : ""}
        </p>
      ) : null}
    </form>
  );
}
