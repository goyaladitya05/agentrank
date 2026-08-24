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
    case "CHANGED":
      return { label: "Changed failure mode", tone: "neutral" };
    default:
      return { label: humanize(direction), tone: "neutral" };
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
