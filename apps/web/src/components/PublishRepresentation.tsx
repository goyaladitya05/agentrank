"use client";

import { useActionState, useState } from "react";

import styles from "@/components/console.module.css";
import type { CompilerMutationState } from "@/lib/compiler-actions";

const IDLE: CompilerMutationState = { ok: false, message: null };

export function PublishRepresentation({
  runId,
  sourceLabel,
  action,
}: {
  runId: string;
  sourceLabel: string;
  action: (
    state: CompilerMutationState,
    formData: FormData,
  ) => CompilerMutationState | Promise<CompilerMutationState>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [state, formAction, pending] = useActionState(action, IDLE);
  if (!confirming) {
    return (
      <button className={styles.button} type="button" onClick={() => setConfirming(true)}>
        Review publication
      </button>
    );
  }
  return (
    <form action={formAction} aria-label="Confirm representation publication">
      <p>
        Publish the immutable representation for source {sourceLabel} from compiler run {runId}?
        This does not rerun a benchmark.
      </p>
      <button className={styles.button} type="submit" disabled={pending}>
        {pending ? "Publishing" : "Publish representation"}
      </button>{" "}
      <button className={styles.textLink} type="button" onClick={() => setConfirming(false)}>
        Cancel
      </button>
      {state.message !== null ? <p role="alert">{state.message}</p> : null}
    </form>
  );
}
