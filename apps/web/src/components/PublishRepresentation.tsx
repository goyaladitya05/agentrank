"use client";

import { useActionState, useState } from "react";

import styles from "@/components/console.module.css";
import { IDLE_MUTATION, type CompilerMutationState } from "@/lib/compiler-mutation";

/**
 * Publishing an agent-ready representation, behind an explicit confirmation.
 *
 * Publication is the one command here that produces a new immutable artifact, so it is never one
 * click away, and the confirmation says what it does and what it does not do. It does not run a
 * benchmark and it makes no claim about performance; a merchant who reads only this sentence
 * should not come away expecting a score to move.
 */

export type PublishAction = (
  state: CompilerMutationState,
) => CompilerMutationState | Promise<CompilerMutationState>;

export function PublishRepresentation({
  runId,
  sourceLabel,
  action,
}: {
  runId: string;
  sourceLabel: string;
  action: PublishAction;
}) {
  const [confirming, setConfirming] = useState(false);
  const [state, formAction, pending] = useActionState(action, IDLE_MUTATION);
  if (!confirming) {
    return (
      <button className={styles.button} type="button" onClick={() => setConfirming(true)}>
        Review publication
      </button>
    );
  }
  return (
    <PublishConfirmation
      runId={runId}
      sourceLabel={sourceLabel}
      action={formAction}
      state={state}
      pending={pending}
      onCancel={() => setConfirming(false)}
    />
  );
}

export function PublishConfirmation({
  runId,
  sourceLabel,
  action,
  state,
  pending,
  onCancel,
}: {
  runId: string;
  sourceLabel: string;
  action: string | (() => void);
  state: CompilerMutationState;
  pending: boolean;
  onCancel?: () => void;
}) {
  return (
    <form action={action} aria-label="Confirm representation publication">
      <p>
        Publish the immutable agent-ready representation for source {sourceLabel} from compiler run{" "}
        <span className={styles.mono}>{runId}</span>?
      </p>
      <p className={styles.reviewMeta}>
        Your reviewed facts become a new representation that never changes afterwards. This does not
        rerun a benchmark and it does not change any price, stock level or order.
      </p>
      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          Publish representation
        </button>
        <button className={styles.textLink} type="button" onClick={onCancel} disabled={pending}>
          Cancel
        </button>
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Publishing the representation
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
