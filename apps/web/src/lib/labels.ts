/**
 * Merchant facing words for everything the diagnostics API names in code.
 *
 * The API is the decision layer for what a merchant is told; this module only translates
 * its vocabulary into readable labels and the restrained visual tones the design system
 * uses. Raw enum values may appear beside a label as secondary technical detail but never
 * as the primary wording. Tones are deliberately separate from meaning: a label always
 * carries the meaning, the tone only supports scanning.
 */

import type {
  ActionabilityValue,
  DiagnosticOwnerValue,
  EvidenceLevelValue,
  SeverityValue,
} from "@/lib/insights/types";

export type Tone = "ok" | "warn" | "fail" | "info" | "neutral";

export const OWNER_LABELS: Record<DiagnosticOwnerValue, string> = {
  MERCHANT_CATALOG: "Your catalog",
  MERCHANT_REVIEW: "Needs merchant review",
  COMPILER: "Compiler",
  BUYER_AGENT: "Buyer agent",
  MODEL_PROVIDER: "Model provider",
  COMMERCE_RUNTIME: "Commerce runtime",
  PAYMENT_PROVIDER: "Payment provider",
  BENCHMARK_INFRASTRUCTURE: "Benchmark infrastructure",
  UNKNOWN: "Unresolved",
};

export const ACTIONABILITY_LABELS: Record<ActionabilityValue, string> = {
  MERCHANT_ACTION: "You can fix this",
  NO_MERCHANT_ACTION: "No action required from you",
  AGENT_SYSTEM_ACTION: "AgentRank system action",
  REVIEW_REQUIRED: "Review needed",
};

/** How loud an actionability is allowed to be. Provider issues never read as alarms here. */
export const ACTIONABILITY_TONES: Record<ActionabilityValue, Tone> = {
  MERCHANT_ACTION: "warn",
  NO_MERCHANT_ACTION: "neutral",
  AGENT_SYSTEM_ACTION: "info",
  REVIEW_REQUIRED: "info",
};

export const SEVERITY_LABELS: Record<SeverityValue, string> = {
  CRITICAL: "Critical",
  HIGH: "High",
  MEDIUM: "Medium",
  LOW: "Low",
};

export const SEVERITY_TONES: Record<SeverityValue, Tone> = {
  CRITICAL: "fail",
  HIGH: "warn",
  MEDIUM: "warn",
  LOW: "neutral",
};

export const EVIDENCE_LEVEL_LABELS: Record<EvidenceLevelValue, string> = {
  TRUSTED_FACT: "Trusted fact",
  DETERMINISTIC_ATTRIBUTION: "Deterministic attribution",
  UNRESOLVED: "Unresolved evidence",
};

export function ownerLabel(owner: string): string {
  return OWNER_LABELS[owner as DiagnosticOwnerValue] ?? humanize(owner);
}

export function actionabilityLabel(actionability: string): string {
  return ACTIONABILITY_LABELS[actionability as ActionabilityValue] ?? humanize(actionability);
}

export function actionabilityTone(actionability: string): Tone {
  return ACTIONABILITY_TONES[actionability as ActionabilityValue] ?? "neutral";
}

export function severityLabel(severity: string): string {
  return SEVERITY_LABELS[severity as SeverityValue] ?? humanize(severity);
}

export function severityTone(severity: string): Tone {
  return SEVERITY_TONES[severity as SeverityValue] ?? "neutral";
}

export function evidenceLevelLabel(level: string): string {
  return EVIDENCE_LEVEL_LABELS[level as EvidenceLevelValue] ?? humanize(level);
}

/** Run and mission statuses as they are persisted. */
export function statusLabel(status: string): { label: string; tone: Tone } {
  switch (status) {
    case "COMPLETED":
    case "PUBLISHED":
      return { label: "Completed", tone: "ok" };
    case "RUNNING":
      return { label: "Running", tone: "info" };
    case "PENDING":
      return { label: "Pending", tone: "neutral" };
    case "ABORTED":
      return { label: "Aborted", tone: "warn" };
    case "SUCCEEDED":
      return { label: "Succeeded", tone: "ok" };
    case "FAILED":
      return { label: "Failed", tone: "warn" };
    case "ABSTAINED":
      return { label: "Abstained", tone: "neutral" };
    case "ERRORED":
      return { label: "Errored", tone: "neutral" };
    default:
      return { label: humanize(status), tone: "neutral" };
  }
}

/**
 * Benchmark designation is an honesty marker, not decoration. Development results must
 * never present themselves as independent evaluation evidence.
 */
export function designationLabel(designation: string | null): {
  label: string;
  tone: Tone;
  note: string;
} {
  switch (designation?.toUpperCase()) {
    case "EVALUATION":
      return {
        label: "Evaluation benchmark",
        tone: "info",
        note: "Run against the evaluation benchmark.",
      };
    case "DEVELOPMENT":
      return {
        label: "Development benchmark",
        tone: "warn",
        note: "Development benchmark result. Not independent evaluation evidence.",
      };
    default:
      return {
        label: designation === null ? "Designation unrecorded" : humanize(designation),
        tone: "neutral",
        note: "This run predates recorded designations or carries an unknown one.",
      };
  }
}

export function conclusionKindLabel(kind: string): { label: string; tone: Tone } {
  switch (kind) {
    case "PARITY":
      return { label: "Parity", tone: "neutral" };
    case "OUTCOME_DIFFERENCES":
      return { label: "Outcome differences", tone: "warn" };
    case "INCOMPLETE":
      return { label: "Incomplete", tone: "neutral" };
    case "NOT_INTERPRETABLE":
      return { label: "Not interpretable", tone: "warn" };
    default:
      return { label: humanize(kind), tone: "neutral" };
  }
}

export function transitionDirectionLabel(direction: string): { label: string; tone: Tone } {
  switch (direction) {
    case "COMPILED_GAIN":
      return { label: "Compiled succeeded, raw did not", tone: "ok" };
    case "COMPILED_LOSS":
      return { label: "Raw succeeded, compiled did not", tone: "warn" };
    case "IMPROVED":
      return { label: "Newly completed", tone: "ok" };
    case "REGRESSED":
      return { label: "No longer completed", tone: "warn" };
    case "CHANGED":
      return { label: "Changed failure mode", tone: "neutral" };
    default:
      return { label: humanize(direction), tone: "neutral" };
  }
}

/**
 * Where one launch has got to. Launch state and run state are different facts and read as
 * different sentences: queued means nothing has executed, and executing means the run beside it
 * carries what is happening.
 */
export function launchStatusLabel(
  status: string,
  failureCode: string | null = null,
): { label: string; tone: Tone } {
  switch (status) {
    case "QUEUED":
      return { label: "Queued", tone: "neutral" };
    case "EXECUTING":
      return { label: "Running", tone: "info" };
    case "COMPLETED":
      return { label: "Completed", tone: "ok" };
    case "FAILED":
      // A launch somebody closed on purpose is not a failure and must not be marked as one. The
      // schema has one settled state for "did not produce a run", so what separates the two is
      // the code beside it, and a merchant who withdrew their own evaluation should not be told
      // it went wrong.
      if (failureCode === "withdrawn_by_merchant") {
        return { label: "Withdrawn", tone: "neutral" };
      }
      if (failureCode === "cancelled_by_operator") {
        return { label: "Cancelled", tone: "neutral" };
      }
      return { label: "Not completed", tone: "warn" };
    default:
      return { label: humanize(status), tone: "neutral" };
  }
}

/**
 * Why one mission ended the way it did, as a phrase rather than as the stored enum.
 *
 * Short on purpose: this appears inside a table cell beside another one of itself, in the
 * before and after columns of a comparison, where a sentence would not fit and two sentences
 * side by side would not be readable.
 */
export function failureReasonLabel(reason: string): string {
  switch (reason) {
    case "DISCOVERY_FAILURE":
      return "nothing was found to buy";
    case "ATTRIBUTE_MISSING":
      return "a required fact is not published";
    case "ATTRIBUTE_UNREADABLE":
      return "a published fact could not be read";
    case "CATEGORY_MISSING":
      return "the category is not published";
    case "INVALID_VARIANT":
      return "the variant chosen was not one on sale";
    case "WRONG_MERCHANT":
      return "the variant chosen belongs to another merchant";
    case "QUANTITY_MISMATCH":
      return "the quantity did not match the mission";
    case "CURRENCY_MISMATCH":
      return "the currency did not match the authorization";
    case "BUDGET_EXCEEDED":
      return "the price was over the authorized amount";
    case "CONSTRAINT_VIOLATION":
      return "the purchase broke a stated constraint";
    case "MANDATE_DENIED":
      return "the authorization refused the purchase";
    case "INVENTORY_UNAVAILABLE":
      return "the stock was not there";
    case "CHECKOUT_CREATION_FAILED":
      return "the checkout could not be created";
    case "PAYMENT_FAILED":
      return "the payment did not succeed";
    case "PAYMENT_UNRESOLVED":
      return "the payment outcome is unresolved";
    case "UNEXPECTED_PURCHASE":
      return "something was bought that should not have been";
    case "MERCHANT_API_ERROR":
      return "the merchant commerce API";
    case "AGENT_REASONING_ERROR":
      return "the AI buyer's own reasoning";
    case "AGENT_EXECUTION_ERROR":
      return "the AI buyer could not complete its own run";
    case "ENFORCEMENT_BYPASSED":
      return "a payment succeeded past a refusal";
    default:
      return humanize(reason).toLowerCase();
  }
}

/** Why a compiler run produced nothing, in words rather than in a code. */
export function compilerFailureLabel(code: string): string {
  switch (code) {
    case "invalid_source_or_candidate":
      return "AgentRank could not read this snapshot as a source document. Supply newer source evidence and compile that.";
    default:
      return humanize(code);
  }
}

/** Why a launch could not produce a finished run, in words rather than in a code. */
export function launchFailureLabel(code: string): string {
  switch (code) {
    case "provider_credential_unavailable":
      return "AgentRank had no credential for the model provider this launch was frozen for, so it was not run with a different one.";
    case "benchmark_world_mismatch":
      return "The benchmark world this launch was frozen against is not the one the operator process holds.";
    case "benchmark_suite_unavailable":
      return "The benchmark suite this launch was frozen against is no longer published.";
    case "buyer_configuration_invalid":
      return "The buyer configuration this launch froze is not one the current AgentRank build can reproduce exactly.";
    case "representation_unavailable":
      return "The representation this launch froze could not be read.";
    case "merchant_source_unavailable":
      return "The merchant information this launch froze could not be read.";
    case "execution_budget_unavailable":
      return "This launch was admitted before AgentRank bounded model spending, so there is no allowance to run it under.";
    // The four below are AgentRank's own execution governance and never a provider fault or a
    // catalog problem. Each says whose decision it was, because a merchant reading "the model
    // provider failed" when nothing was even asked would be reading something untrue.
    case "provider_budget_exhausted":
      return "This evaluation used the whole model request allowance it was launched with, including the requests that were retried, so AgentRank stopped rather than making more. What ran is recorded; what did not run was not measured.";
    case "provider_execution_paused":
      return "AgentRank paused model execution for this provider, so no model request was made. Nothing about your catalog was measured and nothing failed.";
    case "provider_capacity_unavailable":
      return "AgentRank runs a limited number of evaluations against this model provider at once, and it could not start another.";
    case "provider_window_cap_reached":
      return "AgentRank reached its own deployment ceiling for model requests, so it stopped rather than making more.";
    case "run_aborted":
      return "The benchmark run this launch started was stopped and closed by an operator. Nothing was replayed.";
    // Neither of these is a failure. They are the two ways an evaluation that had not started is
    // deliberately put down, and the wording says who did it.
    case "withdrawn_by_merchant":
      return "You withdrew this evaluation before it started. No mission ran, no stock was held and no payment was attempted, so nothing was measured and no earlier evidence changed.";
    case "cancelled_by_operator":
      return "Your operator closed this evaluation before it started, which frees it to be requested again. No mission ran and nothing was measured.";
    default:
      return humanize(code);
  }
}

/**
 * Every methodology caveat the comparison engine can raise, as a heading a merchant can scan.
 * The sentence beside it comes from the API and is never rewritten here.
 */
export function warningLabel(code: string): string {
  switch (code) {
    case "NOT_A_CONTROLLED_EXPERIMENT":
      return "Not a controlled experiment";
    case "SMALL_SAMPLE":
      return "One run on each side";
    case "SUITE_DIFFERS":
      return "Different workload";
    case "ENVIRONMENT_DIFFERS":
      return "Different benchmark world";
    case "EVALUATOR_DIFFERS":
      return "Different marking rules";
    case "CATALOG_PIN_DIFFERS":
      return "Your catalog changed";
    case "EXECUTOR_DIFFERS":
      return "Different buyer";
    case "EXECUTOR_REVISION_DIFFERS":
      return "Buyer code changed";
    case "BUYER_CONFIGURATION_DIFFERS":
      return "Different buyer configuration";
    case "REPRESENTATION_DELIVERY_DIFFERS":
      return "Only one run saw a representation";
    case "RESOLVED_MODEL_MISMATCH":
      return "Different resolved models";
    case "PROVIDER_FAILURES_PRESENT":
      return "Provider failures present";
    case "TOKEN_USAGE_UNAVAILABLE":
      return "Token usage unknown";
    case "RUN_NOT_COMPLETED":
      return "A run did not complete";
    default:
      return humanize(code);
  }
}

/** The counts a comparison publishes, in merchant words rather than as field names. */
export function comparisonCountLabel(key: string): string {
  switch (key) {
    case "missions_total":
      return "Missions";
    case "missions_succeeded":
      return "Compliant purchases";
    case "missions_failed":
      return "Failed missions";
    case "missions_abstained":
      return "Abstentions";
    case "missions_errored":
      return "Missions AgentRank could not measure";
    case "missions_unfinished":
      return "Missions with no outcome";
    case "purchase_missions":
      return "Missions where a purchase was available";
    case "control_missions":
      return "Control missions";
    case "correct_abstentions":
      return "Correct abstentions";
    case "incorrect_abstentions":
      return "Incorrect abstentions";
    case "unsafe_attempts":
      return "Unsafe attempts blocked";
    case "unverified_attempts":
      return "Unverified attempts";
    case "unsafe_completions":
      return "Enforcement escapes";
    case "oracle_disagreements":
      return "Ground truth disagreements";
    case "provider_failure_missions":
      return "Missions with provider errors";
    case "model_invocations":
      return "Provider round trips";
    case "tool_calls":
      return "Tool calls";
    default:
      return humanize(key);
  }
}

/** The two rates a comparison publishes. There is no weighted score to label. */
export function comparisonRateLabel(key: string): string {
  switch (key) {
    case "task_completion_rate":
      return "Task completion";
    case "correct_abstention_rate":
      return "Correct abstention";
    default:
      return humanize(key);
  }
}

const EVENT_TYPE_LABELS: Record<string, string> = {
  MODEL_REQUEST: "Model request",
  MODEL_RESPONSE: "Model response",
  TOOL_CALL: "Tool call",
  TOOL_RESULT: "Tool result",
  TOOL_ERROR: "Tool error",
  AGENT_FINAL: "Agent final",
  AGENT_ABORT: "Agent abort",
  PROVIDER_ERROR: "Provider error",
};

export function traceEventLabel(eventType: string): string {
  return EVENT_TYPE_LABELS[eventType] ?? humanize(eventType);
}

/**
 * The bucket names the API actually sends, which are uppercase on every surface.
 *
 * Two vocabularies reach here and both are wired. A mission diagnosis reports demand as
 * `CAPTURED`, `AT_RISK` or `NOT_MEASURED`; a run comparison reports the four totals a run
 * carries, `POTENTIAL`, `CAPTURED`, `LOST` and `NOT_MEASURED`. AT_RISK and LOST are the same
 * bucket named from two directions and both read as lost demand, which is what they are.
 */
const DEMAND_BUCKET_LABELS: Record<string, string> = {
  POTENTIAL: "Simulated potential demand",
  CAPTURED: "Simulated captured demand",
  AT_RISK: "Simulated lost demand",
  LOST: "Simulated lost demand",
  NOT_MEASURED: "Simulated unmeasured demand",
};

export function demandBucketLabel(bucket: string): string {
  return (
    DEMAND_BUCKET_LABELS[bucket.toUpperCase()] ??
    `Simulated ${humanize(bucket).toLowerCase()} demand`
  );
}

/** Fallback that keeps an unexpected value readable without pretending to understand it. */
/**
 * What one generated mission kind is, in words rather than in the code the backend names it by.
 *
 * A merchant reading their evaluation setup should learn what the benchmark asks of them.
 * `humanize` is the fallback rather than an omission: a setup built by an older generator may
 * carry a kind this build has no sentence for, and rendering it readably beats hiding it.
 */
export function missionFamilyLabel(family: string): string {
  return MISSION_FAMILY_LABELS[family] ?? humanize(family);
}

const MISSION_FAMILY_LABELS: Record<string, string> = {
  CATEGORY_PURCHASE: "Buy something from a category",
  BUDGET_CONSTRAINED_PURCHASE: "Buy the one that fits the budget",
  MULTI_UNIT_PURCHASE: "Buy more than one",
  SPECIFICATION_PURCHASE: "Buy one meeting a stated specification",
  BUDGET_ABSTENTION: "Decline when nothing is affordable",
  STOCK_ABSTENTION: "Decline when you cannot supply the quantity",
  UNAVAILABLE_ABSTENTION: "Decline when a category is out of stock",
  SPECIFICATION_ABSTENTION: "Decline when the stated specification is unavailable",
  POLICY_CONSTRAINT: "Answer a policy question",
};

export function humanize(value: string): string {
  const words = value
    .toLowerCase()
    .split(/[_\s]+/)
    .filter((word) => word.length > 0);
  const first = words[0];
  if (first === undefined) {
    return value;
  }
  return [first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(" ");
}
