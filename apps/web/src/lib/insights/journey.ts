/**
 * How far one shopping agent actually got, derived from what the run recorded.
 *
 * A shopping attempt passes through four things a merchant can picture: it has to find
 * candidates at all, it has to establish that a candidate meets what the shopper asked for,
 * it has to choose one, and it has to get through checkout and payment. AgentRank already
 * records which of those happened, in the diagnosis codes the engine assigns and in the
 * commerce artifacts a mission cites as evidence. This module reads that record; it never
 * guesses and never invents a stage a run did not evidence.
 *
 * The stage a journey stops at is therefore a fact about the recorded attempt, not a
 * narration of it. Nothing here reads a model's reasoning, and there is none to read: the
 * evidence is the commerce kernel's own artifacts plus the diagnosis the trusted evaluator
 * assigned.
 */

import type { MissionDiagnosis } from "@/lib/insights/types";

export const STAGES = ["DISCOVER", "UNDERSTAND", "SELECT", "CHECKOUT"] as const;
export type Stage = (typeof STAGES)[number];

/**
 * What became of one attempt.
 *
 * `completed` is a purchase the evaluator marked correct. `declined` is a correct abstention,
 * which is a success and is drawn as a deliberate stop rather than as a failure. `blocked` is
 * a failure the merchant owns. `interrupted` is a provider or infrastructure fault, which is
 * categorically not the merchant's and is drawn differently. `unmeasured` is everything
 * AgentRank could not mark either way.
 */
export type JourneyOutcome = "completed" | "declined" | "blocked" | "interrupted" | "unmeasured";

export interface ScenarioJourney {
  readonly missionRunId: string;
  readonly missionKey: string;
  readonly outcome: JourneyOutcome;
  /** The last stage the recorded evidence supports. Every earlier stage was reached. */
  readonly reached: Stage;
  /** One short merchant sentence for where it stopped, or null when it finished. */
  readonly stoppedBecause: string | null;
}

/** Codes that say the attempt never found anything to consider. */
const DISCOVERY_CODES = new Set(["DISCOVERY_FAILED", "CATEGORY_NOT_PUBLISHED"]);

/** Codes that say a candidate was found but a required fact could not be established. */
const UNDERSTAND_CODES = new Set([
  "ATTRIBUTE_NOT_PUBLISHED",
  "ATTRIBUTE_UNREADABLE",
  "GROUND_TRUTH_DISAGREEMENT",
]);

/** Codes that say the attempt reached the commerce kernel and was stopped there. */
const CHECKOUT_CODES = new Set([
  "CHECKOUT_REFUSED",
  "STOCK_UNAVAILABLE",
  "PAYMENT_FAILED",
  "PAYMENT_UNRESOLVED",
  "MERCHANT_SURFACE_ERROR",
  "AUTHORIZATION_DENIED_COMPLIANT_ATTEMPT",
]);

/** Codes owned by the model provider or by AgentRank's own infrastructure. */
const INTERRUPTION_CODES = new Set([
  "PROVIDER_OUTAGE_TERMINATED_MISSION",
  "PROVIDER_THROTTLE_RECOVERED",
  "AGENT_EXECUTION_ERROR",
  "BENCHMARK_INFRASTRUCTURE_ERROR",
]);

/** Which commerce artifacts a mission cited, which is how far it demonstrably got. */
function citedKinds(diagnosis: MissionDiagnosis): Set<string> {
  const kinds = new Set<string>();
  for (const finding of diagnosis.findings) {
    for (const reference of finding.evidence) {
      kinds.add(reference.kind);
    }
  }
  return kinds;
}

function outcomeOf(diagnosis: MissionDiagnosis): JourneyOutcome {
  if (diagnosis.status === "SUCCEEDED") {
    return "completed";
  }
  if (diagnosis.status === "ABSTAINED") {
    // The evaluator marks an abstention correct or incorrect; a mission whose primary code is a
    // discovery failure abstained when something was in fact available, which is a merchant
    // problem rather than a deliberate decline.
    return diagnosis.primary_code === null ? "declined" : "blocked";
  }
  if (diagnosis.status === "ERRORED") {
    return "unmeasured";
  }
  const owner = diagnosis.findings.find(
    (finding) => finding.code === diagnosis.primary_code,
  )?.owner;
  if (
    (diagnosis.primary_code !== null && INTERRUPTION_CODES.has(diagnosis.primary_code)) ||
    owner === "MODEL_PROVIDER" ||
    owner === "BENCHMARK_INFRASTRUCTURE"
  ) {
    return "interrupted";
  }
  return "blocked";
}

/**
 * The furthest stage the recorded evidence supports.
 *
 * Evidence first, because a cited checkout or payment attempt is the commerce kernel's own
 * record that the attempt got that far. The diagnosis code decides only where evidence is
 * silent, and an attempt that finished is at checkout by definition.
 */
function reachedStage(diagnosis: MissionDiagnosis, outcome: JourneyOutcome): Stage {
  if (outcome === "completed") {
    return "CHECKOUT";
  }
  const kinds = citedKinds(diagnosis);
  if (kinds.has("checkout") || kinds.has("payment_attempt")) {
    return "CHECKOUT";
  }
  const code = diagnosis.primary_code;
  if (code !== null && CHECKOUT_CODES.has(code)) {
    return "CHECKOUT";
  }
  if (kinds.has("variant")) {
    return "SELECT";
  }
  if (code !== null && UNDERSTAND_CODES.has(code)) {
    return "UNDERSTAND";
  }
  if (code !== null && DISCOVERY_CODES.has(code)) {
    return "DISCOVER";
  }
  // A correct decline is a decision taken after understanding what was on the shelf, which is
  // exactly what makes it correct. Anything else with no evidence and no located code is only
  // known to have started.
  return outcome === "declined" ? "UNDERSTAND" : "DISCOVER";
}

/** The merchant sentence for where an unfinished attempt stopped. */
function stoppedBecause(diagnosis: MissionDiagnosis, outcome: JourneyOutcome): string | null {
  if (outcome === "completed") {
    return null;
  }
  if (outcome === "declined") {
    return "Declined correctly. Nothing on the shelf met what the shopper asked for.";
  }
  const primary = diagnosis.findings.find((finding) => finding.code === diagnosis.primary_code);
  return primary?.summary ?? "AgentRank recorded no diagnosis for this scenario.";
}

export function scenarioJourney(diagnosis: MissionDiagnosis): ScenarioJourney {
  const outcome = outcomeOf(diagnosis);
  return {
    missionRunId: diagnosis.mission_run_id,
    missionKey: diagnosis.mission_key,
    outcome,
    reached: reachedStage(diagnosis, outcome),
    stoppedBecause: stoppedBecause(diagnosis, outcome),
  };
}

export function scenarioJourneys(
  missions: readonly MissionDiagnosis[],
): readonly ScenarioJourney[] {
  return missions.map(scenarioJourney);
}

/** How many stages of the four a journey covered, for drawing its track. */
export function stagesReached(journey: ScenarioJourney): number {
  return STAGES.indexOf(journey.reached) + 1;
}
