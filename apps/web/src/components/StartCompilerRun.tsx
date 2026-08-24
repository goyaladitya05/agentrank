"use client";

import Link from "next/link";
import { useActionState } from "react";

import styles from "@/components/console.module.css";
import { IDLE_COMPILE, type CompileState } from "@/lib/source-mutation";

/**
 * Running the deterministic compiler over one immutable source snapshot.
 *
 * A separate command from supplying the evidence, because they are separate decisions. A
 * snapshot is what the merchant says about their catalog; a run is a reading of that statement
 * that produces facts the merchant then has to answer for one by one. A workflow that did both
 * from one click would leave a merchant unable to say which of the two they meant.
 *
 * The confirmation states what a run does and what it does not do. It reads source evidence and
 * proposes facts; it publishes nothing, changes no price or stock level, and starts no benchmark.
 * It makes no claim that anything will improve, because nothing here knows that.
 *
 * Whether this snapshot can still be compiled is a prop rather than a condition the page decides
 * before rendering this at all, and that is load bearing. Running the compiler revalidates the
 * page it was run from, so a component the page only renders while the snapshot is uncompiled
 * would unmount at exactly the moment it had something to say, and the merchant would be left
 * reading a refreshed page with no acknowledgement that their own click caused it.
 */

export type CompileAction = (state: CompileState) => CompileState | Promise<CompileState>;

export function StartCompilerRun({
  sourceLabel,
  compilable,
  existingRunId,
  action,
}: {
  sourceLabel: string;
  compilable: boolean;
  existingRunId: string | null;
  action: CompileAction;
}) {
  const [state, formAction, pending] = useActionState(action, IDLE_COMPILE);
  if (state.ok) {
    return <CompileAccepted state={state} />;
  }
  if (!compilable) {
    return <AlreadyCompiled existingRunId={existingRunId} />;
  }
  return (
    <CompileConfirmation
      sourceLabel={sourceLabel}
      action={formAction}
      state={state}
      pending={pending}
    />
  );
}

/**
 * What a snapshot that has already been read says.
 *
 * Not a refusal and not an error. Deterministic compilation of one snapshot under one
 * configuration produces the same facts every time, so a second run would be the same run, and
 * the useful thing to offer is the one that exists.
 */
export function AlreadyCompiled({ existingRunId }: { existingRunId: string | null }) {
  return (
    <>
      <p>This snapshot has already been read by the compiler.</p>
      <p className={styles.reviewMeta}>
        The compiler is deterministic, so reading the same snapshot the same way again produces the
        same facts. To get different proposals, supply newer source evidence.
      </p>
      {existingRunId === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/compiler/runs/${encodeURIComponent(existingRunId)}`}
          >
            Review the compiler run for this snapshot
          </Link>
        </p>
      )}
    </>
  );
}

export function CompileAccepted({ state }: { state: CompileState }) {
  return (
    <div role="status">
      <p>Compiler run finished. The facts it proposed are waiting for your review.</p>
      {state.runId === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/compiler/runs/${encodeURIComponent(state.runId)}`}
          >
            Review this compiler run
          </Link>
        </p>
      )}
    </div>
  );
}

export function CompileConfirmation({
  sourceLabel,
  action,
  state,
  pending,
}: {
  sourceLabel: string;
  action: string | (() => void);
  state: CompileState;
  pending: boolean;
}) {
  return (
    <form action={action} aria-label="Run the compiler">
      <p>Read source snapshot {sourceLabel} with the deterministic compiler?</p>
      <ul className={styles.launchTerms}>
        <li>
          The compiler proposes typed facts and cites the source field behind each one. You decide
          what becomes published truth.
        </li>
        <li>This publishes nothing and starts no benchmark. Both remain separate commands.</li>
        <li>No price, stock level or order changes.</li>
        <li>
          The same snapshot read the same way is the same run, so asking twice cannot produce two.
        </li>
      </ul>
      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          Run the compiler
        </button>
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Running the compiler
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
