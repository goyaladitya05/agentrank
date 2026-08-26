"use client";

import { useActionState, useState } from "react";

import styles from "@/components/console.module.css";
import { IDLE_LAUNCH, type LaunchState } from "@/lib/evaluation-mutation";

/**
 * Putting down a queued evaluation.
 *
 * The exit from the one state this console could not leave. A queued launch waits for an
 * operator process to dispatch it, and while it waits the merchant can neither request another
 * evaluation nor build a new evaluation setup, so a deployment with no worker configured for the
 * frozen executor left them looking at a spinner with no way out.
 *
 * Two steps, like every other command here that cannot be undone. The first press asks; the
 * second withdraws. Nothing was measured, so the confirmation says exactly that rather than
 * warning about consequences a queued launch does not have.
 */

export type WithdrawAction = (state: LaunchState) => LaunchState | Promise<LaunchState>;

export function WithdrawEvaluation({ action }: { action: WithdrawAction }) {
  const [confirming, setConfirming] = useState(false);
  const [state, formAction, pending] = useActionState(action, IDLE_LAUNCH);

  if (!confirming) {
    return (
      <div className={styles.buttonRow}>
        <button
          className={styles.textLink}
          type="button"
          onClick={() => {
            setConfirming(true);
          }}
        >
          Withdraw this evaluation
        </button>
      </div>
    );
  }
  return (
    <form action={formAction} aria-label="Withdraw this evaluation">
      <p className={styles.reviewMeta}>
        Withdrawing closes this request. No mission has run, no stock was held and no payment was
        attempted, so nothing is being undone and no evidence changes. It stays in your evaluation
        history as a request you withdrew, and you can ask for another evaluation afterwards.
      </p>
      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          Withdraw evaluation
        </button>
        <button
          className={styles.textLink}
          type="button"
          onClick={() => {
            setConfirming(false);
          }}
          disabled={pending}
        >
          Keep waiting
        </button>
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Withdrawing this evaluation
        </p>
      ) : null}
      {state.message !== null && !pending ? (
        <p className={styles.mutationAlert} role="alert">
          {state.message}
          {state.stale && !state.unknown ? " The state shown here is current." : ""}
        </p>
      ) : null}
    </form>
  );
}
