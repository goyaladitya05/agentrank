/**
 * Product level derivations over decoded overview and run data.
 *
 * The backend owns every fact and its meaning; this module only composes those facts into
 * the few sentences and counts the overview needs to answer "do I need to do something?"
 * before anything else. Nothing here reinterprets ownership or actionability: it counts
 * what the API already decided.
 */

import type { Tone } from "@/lib/labels";
import type { MerchantFinding } from "@/lib/insights/types";

export interface AttentionSummary {
  readonly totalFindings: number;
  readonly merchantAction: number;
  readonly reviewRequired: number;
  /** Provider, benchmark or otherwise external issues that need no merchant action. */
  readonly systemOrExternal: number;
  readonly agentSystemAction: number;
}

export function attentionSummary(findings: readonly MerchantFinding[]): AttentionSummary {
  let merchantAction = 0;
  let reviewRequired = 0;
  let agentSystemAction = 0;
  let systemOrExternal = 0;
  for (const finding of findings) {
    switch (finding.actionability) {
      case "MERCHANT_ACTION":
        merchantAction += 1;
        break;
      case "REVIEW_REQUIRED":
        reviewRequired += 1;
        break;
      case "AGENT_SYSTEM_ACTION":
        agentSystemAction += 1;
        break;
      case "NO_MERCHANT_ACTION":
      default:
        // NO_MERCHANT_ACTION findings are informational regardless of owner.
        systemOrExternal += 1;
        break;
    }
  }
  return {
    totalFindings: findings.length,
    merchantAction,
    reviewRequired,
    agentSystemAction,
    systemOrExternal,
  };
}

/**
 * One honest paragraph about whether the merchant needs to act. Provider outages and
 * benchmark faults are named as external rather than folded into merchant work.
 */
export function attentionSentences(summary: AttentionSummary): string[] {
  if (summary.totalFindings === 0) {
    return [];
  }
  const sentences: string[] = [];
  const parts: string[] = [];
  if (summary.merchantAction > 0) {
    parts.push(
      `${String(summary.merchantAction)} need${summary.merchantAction === 1 ? "s" : ""} your action`,
    );
  }
  if (summary.reviewRequired > 0) {
    parts.push(
      `${String(summary.reviewRequired)} need${summary.reviewRequired === 1 ? "s" : ""} review`,
    );
  }
  if (summary.agentSystemAction > 0) {
    parts.push(
      `${String(summary.agentSystemAction)} ${summary.agentSystemAction === 1 ? "is" : "are"} AgentRank system work`,
    );
  }
  if (summary.systemOrExternal > 0) {
    parts.push(
      `${String(summary.systemOrExternal)} ${summary.systemOrExternal === 1 ? "is" : "are"} external or informational`,
    );
  }
  sentences.push(
    `${String(summary.totalFindings)} finding${summary.totalFindings === 1 ? "" : "s"} on this run: ${parts.join("; ")}.`,
  );
  if (summary.merchantAction > 0) {
    sentences.push("Start with the findings marked as yours below.");
  } else {
    sentences.push("No finding requires action from you.");
  }
  return sentences;
}

export interface SafetyReading {
  readonly text: string;
  readonly tone: Tone;
}

/**
 * Safety is reported apart from completion metrics and never flattened into them. A
 * blocked attempt is enforcement working; an escape is money moving past a refusal and is
 * the most serious reading this console can produce.
 */
export function safetyReading(
  unsafeAttempts: number,
  unsafeCompletions: number,
  unverifiedAttempts: number,
): SafetyReading | null {
  if (unsafeCompletions > 0) {
    return {
      text: `${plural(unsafeCompletions, "safety escape")} recorded. Purchases completed past a refusal.`,
      tone: "fail",
    };
  }
  if (unverifiedAttempts > 0) {
    return {
      text: `${plural(unverifiedAttempts, "unsafe attempt")} could not be verified against merchant data.`,
      tone: "warn",
    };
  }
  if (unsafeAttempts > 0) {
    return {
      text: `${plural(unsafeAttempts, "unsafe purchase attempt")}, all blocked before money moved.`,
      tone: "ok",
    };
  }
  return null;
}

export function providerSentence(providerFailureMissions: number): string | null {
  if (providerFailureMissions <= 0) {
    return null;
  }
  return (
    `${plural(providerFailureMissions, "mission")} ended on model provider failures. ` +
    "No merchant action is required."
  );
}

function plural(count: number, noun: string): string {
  return count === 1 ? `1 ${noun}` : `${String(count)} ${noun}s`;
}
