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
export function launchStatusLabel(status: string): { label: string; tone: Tone } {
  switch (status) {
    case "QUEUED":
      return { label: "Queued", tone: "neutral" };
    case "EXECUTING":
      return { label: "Running", tone: "info" };
    case "COMPLETED":
      return { label: "Completed", tone: "ok" };
    case "FAILED":
      return { label: "Not completed", tone: "warn" };
    default:
      return { label: humanize(status), tone: "neutral" };
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
    case "run_aborted":
      return "The benchmark run this launch started was stopped and closed by an operator. Nothing was replayed.";
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

const DEMAND_BUCKET_LABELS: Record<string, string> = {
  potential: "Simulated potential demand",
  captured: "Simulated captured demand",
  lost: "Simulated lost demand",
  not_measured: "Simulated unmeasured demand",
};

export function demandBucketLabel(bucket: string): string {
  return DEMAND_BUCKET_LABELS[bucket] ?? `Simulated ${humanize(bucket).toLowerCase()} demand`;
}

/** Fallback that keeps an unexpected value readable without pretending to understand it. */
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
