import { STAGES, stagesReached, type ScenarioJourney } from "@/lib/insights/journey";

import styles from "./journey.module.css";

const STAGE_LABEL: Record<string, string> = {
  DISCOVER: "Discover",
  UNDERSTAND: "Understand",
  SELECT: "Select",
  CHECKOUT: "Checkout",
};

const OUTCOME_WORD: Record<ScenarioJourney["outcome"], string> = {
  completed: "Bought",
  declined: "Declined",
  blocked: "Blocked",
  interrupted: "Interrupted",
  unmeasured: "Not measured",
};

/**
 * AgentRank's signature graphic: one row per shopping scenario, each row a journey through
 * the four stages of an attempt, stopping where the recorded evidence says it stopped.
 *
 * Every cell is a real stage of a real attempt. A filled cell was reached, a hollow one was
 * not, and the cell where a journey ends carries the reason beside it. Nothing here is
 * decorative and nothing is invented: an empty run draws nothing rather than a placeholder.
 */
export function ScenarioJourneys({
  journeys,
  caption,
}: {
  journeys: readonly ScenarioJourney[];
  caption?: string;
}) {
  if (journeys.length === 0) {
    return null;
  }
  return (
    <figure className={styles.figure}>
      <div className={styles.headerRow} aria-hidden="true">
        <span className={styles.headScenario}>Scenario</span>
        <div className={styles.headStages}>
          {STAGES.map((stage) => (
            <span key={stage} className={styles.headStage}>
              {STAGE_LABEL[stage]}
            </span>
          ))}
        </div>
        <span className={styles.headOutcome}>Outcome</span>
      </div>
      <ol className={styles.list}>
        {journeys.map((journey, index) => (
          <JourneyRow key={journey.missionRunId} journey={journey} index={index} />
        ))}
      </ol>
      {caption === undefined ? null : <figcaption className={styles.caption}>{caption}</figcaption>}
    </figure>
  );
}

function JourneyRow({ journey, index }: { journey: ScenarioJourney; index: number }) {
  const reached = stagesReached(journey);
  const label = `${journey.missionKey}: ${OUTCOME_WORD[journey.outcome]}, reached ${
    STAGE_LABEL[journey.reached] ?? journey.reached
  }`;
  return (
    <li
      className={styles.row}
      data-outcome={journey.outcome}
      style={{ animationDelay: `${String(60 + index * 70)}ms` }}
    >
      <span className={styles.scenario} title={journey.missionKey}>
        {journey.missionKey}
      </span>
      <div className={styles.stages} role="img" aria-label={label}>
        {STAGES.map((stage, position) => {
          const state =
            position < reached - 1 ? "passed" : position === reached - 1 ? "final" : "unreached";
          return (
            <span key={stage} className={styles.cell} data-state={state}>
              <span className={styles.cellLabel}>{STAGE_LABEL[stage]}</span>
            </span>
          );
        })}
      </div>
      <span className={styles.outcome}>{OUTCOME_WORD[journey.outcome]}</span>
      {journey.stoppedBecause === null ? null : (
        <p className={styles.reason}>{journey.stoppedBecause}</p>
      )}
    </li>
  );
}
