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
  sourceLabel,
  action,
}: {
  sourceLabel: string;
  action: PublishAction;
}) {
  const [confirming, setConfirming] = useState(false);
  const [state, formAction, pending] = useActionState(action, IDLE_MUTATION);
  if (!confirming) {
    return (
      <button className={styles.primaryButton} type="button" onClick={() => setConfirming(true)}>
        Publish fixes
      </button>
    );
  }
  return (
    <PublishConfirmation
      sourceLabel={sourceLabel}
      action={formAction}
      state={state}
      pending={pending}
      onCancel={() => setConfirming(false)}
    />
  );
}

export function PublishConfirmation({
  sourceLabel,
  action,
  state,
  pending,
  onCancel,
}: {
  sourceLabel: string;
  action: string | (() => void);
  state: CompilerMutationState;
  pending: boolean;
  onCancel?: () => void;
}) {
  return (
    <form action={action} aria-label="Confirm representation publication">
      <p>Publish your reviewed fixes from {sourceLabel}?</p>
      <p className={styles.reviewMeta}>
        The facts you approved become a new agent-ready description of your store, which never
        changes afterwards. This does not rerun a benchmark and it does not change any price, stock
        level or order. Measuring the difference is the next, separate step.
      </p>
      <div className={styles.buttonRow}>
        <button className={styles.primaryButton} type="submit" disabled={pending}>
          Publish fixes
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
