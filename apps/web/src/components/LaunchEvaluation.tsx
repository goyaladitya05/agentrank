"use client";

import Link from "next/link";
import { useActionState, useState } from "react";

import styles from "@/components/console.module.css";
import { IDLE_LAUNCH, type LaunchState } from "@/lib/evaluation-mutation";
import type { EvaluationPreflight } from "@/lib/evaluation";

/**
 * Asking AgentRank to measure a published representation again.
 *
 * The most expensive command in this console, so it is never one click away. The preflight is
 * shown first and states exactly what will be evaluated and what it will be evaluated with;
 * confirming is a second, deliberate act.
 *
 * What the confirmation says is as important as what it does. It says this is a new run, that
 * previous evidence is unchanged, that a model provider can fail and that the result reflects
 * that when it does, and that launching can consume model quota. It states no cost in currency,
 * because AgentRank has no pricing data and a figure invented from none would be the most
 * confident thing on the page. Execution bounds are published instead, and those are checkable.
 *
 * Split in two so that every state a merchant can land in, including a refusal and a lost
 * response, is renderable in a test without driving a browser.
 */

export type LaunchAction = (state: LaunchState) => LaunchState | Promise<LaunchState>;

export function LaunchEvaluation({
  preflight,
  action,
}: {
  preflight: EvaluationPreflight;
  action: LaunchAction;
}) {
  const [confirming, setConfirming] = useState(false);
  const [state, formAction, pending] = useActionState(action, IDLE_LAUNCH);
  if (state.ok) {
    return <LaunchAccepted state={state} />;
  }
  if (!confirming) {
    return (
      <button className={styles.button} type="button" onClick={() => setConfirming(true)}>
        Review re-evaluation
      </button>
    );
  }
  return (
    <LaunchConfirmation
      preflight={preflight}
      action={formAction}
      state={state}
      pending={pending}
      onCancel={() => setConfirming(false)}
    />
  );
}

/**
 * What a merchant is told when the launch was accepted.
 *
 * A form that simply closed would leave the most expensive command in this console with no
 * acknowledgement at all. This says what was accepted and what has not happened yet, and links
 * to the launch, which is the only page that knows anything further.
 */
export function LaunchAccepted({ state }: { state: LaunchState }) {
  return (
    <div role="status">
      <p>Re-evaluation requested. Nothing has been executed yet.</p>
      {state.launchId === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/evaluations/${encodeURIComponent(state.launchId)}`}
          >
            Follow this re-evaluation
          </Link>
        </p>
      )}
    </div>
  );
}

export function LaunchConfirmation({
  preflight,
  action,
  state,
  pending,
  onCancel,
}: {
  preflight: EvaluationPreflight;
  action: string | (() => void);
  state: LaunchState;
  pending: boolean;
  onCancel?: () => void;
}) {
  const model =
    preflight.buyer_profile === "AI_BUYER"
      ? `${preflight.provider ?? "a model provider"} model ${preflight.requested_model ?? ""}`.trim()
      : "AgentRank's deterministic reference buyer";
  return (
    <form action={action} aria-label="Confirm re-evaluation">
      <p>
        Run {preflight.suite_label ?? "the benchmark suite"} against representation{" "}
        <span className={styles.mono}>{preflight.representation_label}</span> with {model}?
      </p>
      <ul className={styles.launchTerms}>
        <li>
          {preflight.mission_count === null
            ? "Every mission in the suite is executed."
            : `${String(preflight.mission_count)} missions are executed, one at a time.`}
        </li>
        <li>
          This starts a new benchmark run. Every previous run and its findings stay exactly as they
          are.
        </li>
        {preflight.baseline_run_id === null ? (
          <li>
            You have no earlier completed run of this suite, so there will be nothing to compare
            against yet.
          </li>
        ) : (
          <li>The result will be shown beside your most recent completed run of this suite.</li>
        )}
        {preflight.buyer_profile === "AI_BUYER" ? (
          <li>
            Launching sends requests to your model provider and can consume quota or incur cost
            there. AgentRank does not estimate that amount.
          </li>
        ) : (
          <li>
            No AI model provider is configured, so this run uses AgentRank&apos;s deterministic
            reference buyer. It is not an AI agent, it reads structured commerce fields a real
            storefront does not publish, and it does not read your published representation. Its
            result says whether the benchmark path works, and is not evidence about agents.
          </li>
        )}
        {preflight.buyer_profile === "AI_BUYER" ? (
          <li>
            A model provider outage affects the result. Missions it ends are reported as provider
            failures rather than as problems with your catalog.
          </li>
        ) : null}
        <ExecutionBounds preflight={preflight} />
      </ul>
      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          Request re-evaluation
        </button>
        {onCancel === undefined ? null : (
          <button className={styles.textLink} type="button" onClick={onCancel} disabled={pending}>
            Cancel
          </button>
        )}
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Requesting the re-evaluation
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

function ExecutionBounds({ preflight }: { preflight: EvaluationPreflight }) {
  if (preflight.buyer_profile !== "AI_BUYER") {
    return null;
  }
  const bounds = [
    preflight.max_model_turns === null
      ? null
      : `${String(preflight.max_model_turns)} model turns per mission`,
    preflight.max_tool_calls === null
      ? null
      : `${String(preflight.max_tool_calls)} tool calls per mission`,
    preflight.mission_deadline_seconds === null
      ? null
      : `${String(preflight.mission_deadline_seconds)}s per mission`,
  ].filter((bound): bound is string => bound !== null);
  if (bounds.length === 0) {
    return null;
  }
  return <li>Execution is bounded at {bounds.join(", ")}.</li>;
}
