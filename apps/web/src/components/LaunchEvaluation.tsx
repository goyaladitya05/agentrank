"use client";

import Link from "next/link";
import { useActionState, useState } from "react";

import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { IDLE_LAUNCH, type LaunchState } from "@/lib/evaluation-mutation";
import type { EvaluationPreflight } from "@/lib/evaluation";

/**
 * Asking AgentRank to run one benchmark evaluation.
 *
 * The most expensive command in this console, so it is never one click away. The preflight is
 * shown first and states exactly what will be evaluated and what it will be evaluated with;
 * confirming is a second, deliberate act.
 *
 * Two commands share this form and the wording follows the purpose the server resolved. A first
 * evaluation says it measures the merchant as they are and that there will be nothing to
 * compare it with; a re-evaluation names the artifact under test. Neither is described in the
 * other's words.
 *
 * What the confirmation says is as important as what it does. It says this is a new run, that
 * previous evidence is unchanged, that a model provider can fail and that the result reflects
 * that when it does, and that launching can consume model quota. It states no cost in currency,
 * because AgentRank has no pricing data and a figure invented from none would be the most
 * confident thing on the page. Execution bounds are published instead, and those are checkable.
 *
 * The largest of those bounds is the number of model requests this evaluation may make, and it
 * is stated with the thing that makes it easy to misread: a request that fails and is retried
 * counts again. A merchant told "14 missions" and left to infer fourteen model requests would be
 * wrong by whatever a rate limited provider costs, which on the one real pilot AgentRank has run
 * was most of the traffic.
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
  const initial = preflight.purpose === "INITIAL";
  if (!confirming) {
    return (
      <button
        className={merchant.primaryButton}
        type="button"
        onClick={() => setConfirming(true)}
      >
        {initial ? "Run evaluation" : "Measure again"}
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
      <p>Evaluation requested. Nothing has been executed yet.</p>
      {state.launchId === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/evaluations/${encodeURIComponent(state.launchId)}`}
          >
            Follow this evaluation
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
  const initial = preflight.purpose === "INITIAL";
  const model =
    preflight.buyer_profile === "AI_BUYER"
      ? `${preflight.provider ?? "a model provider"} model ${preflight.requested_model ?? ""}`.trim()
      : "AgentRank's deterministic reference buyer";
  return (
    <form
      action={action}
      aria-label={initial ? "Confirm first evaluation" : "Confirm re-evaluation"}
    >
      {initial ? (
        <p>
          Run {preflight.suite_label ?? "the benchmark suite"} against your merchant as it is now,
          using your merchant information{" "}
          <span className={styles.mono}>{preflight.source_snapshot_label ?? ""}</span> with {model}?
        </p>
      ) : (
        <p>
          Run the same shopping scenarios against your published store description with {model}?
        </p>
      )}
      <ul className={styles.launchTerms}>
        <li>
          {preflight.mission_count === null
            ? "Every shopping scenario in the suite is executed."
            : `${String(preflight.mission_count)} shopping scenarios are executed, one at a time.`}
        </li>
        {initial ? (
          <li>
            The buyer reads the ordinary storefront. Nothing is compiled for this run, so it
            measures how AgentRank finds your merchant today.
          </li>
        ) : null}
        <li>
          {initial
            ? "This creates your first benchmark result."
            : "This starts a new benchmark run. Every previous run and its findings stay exactly as they are."}
        </li>
        {initial ? (
          <li>
            You have no earlier result, so this one will not be shown beside anything. AgentRank
            does not report a change where there is nothing to change from.
          </li>
        ) : preflight.baseline_run_id === null ? (
          <li>
            You have no earlier completed run of this suite, so there will be nothing to compare
            against yet.
          </li>
        ) : preflight.baseline_surface_matches === false ? (
          <li>
            Your most recent completed run of this suite measured a different kind of surface, so
            this result will not be shown beside it. Reading a difference between those two is what
            a controlled experiment is for.
          </li>
        ) : (
          <li>
            The result will be read against your most recent completed run of this suite, if the two
            turn out to have measured the same thing.
          </li>
        )}
        <li>Running an evaluation does not change your prices, inventory or any payment.</li>
        {preflight.buyer_profile === "AI_BUYER" ? (
          <li>
            Launching sends requests to your model provider and can consume quota or incur cost
            there. AgentRank does not estimate that amount.
          </li>
        ) : (
          <li>
            No AI model provider is configured, so this run uses AgentRank&apos;s deterministic
            reference buyer. It is not an AI agent, it reads structured commerce fields a real
            storefront does not publish, and it reads neither your storefront nor any published
            representation. Its result says whether the benchmark path works, and is not evidence
            about agents.
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
          {initial ? "Request first evaluation" : "Request re-evaluation"}
        </button>
        {onCancel === undefined ? null : (
          <button className={styles.textLink} type="button" onClick={onCancel} disabled={pending}>
            Cancel
          </button>
        )}
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Requesting the evaluation
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
  return (
    <>
      {bounds.length === 0 ? null : <li>Execution is bounded at {bounds.join(", ")}.</li>}
      <RequestAllowance preflight={preflight} />
    </>
  );
}

/**
 * The model request ceiling, and the sentence that keeps it from being misread.
 *
 * A request that times out or is rate limited and then retried is a second request, and it costs
 * whatever the provider charges for it. AgentRank stops at this number rather than making
 * another, which is why it is worth a merchant knowing before they commit rather than after an
 * evaluation stops part way through.
 */
function RequestAllowance({ preflight }: { preflight: EvaluationPreflight }) {
  if (preflight.max_provider_requests === null) {
    return null;
  }
  return (
    <li>
      AgentRank will make at most {String(preflight.max_provider_requests)} model requests for this
      evaluation. Retries count against that: a request that times out or is rate limited and is
      tried again is another request. If the allowance runs out, the evaluation stops rather than
      making more, and the result says so.
    </li>
  );
}
