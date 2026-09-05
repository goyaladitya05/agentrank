import { STAGES, scenarioName, stagesReached, type ScenarioJourney } from "@/lib/insights/journey";

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
 * The scenario board: one row per shopping scenario, each row a journey through the four
 * stages of an attempt, filled to the stage the recorded evidence says it reached.
 *
 * Every cell is a real stage of a real attempt and the track is described in one sentence
 * for anyone who cannot see it. Nothing here is decorative and nothing is invented: an empty
 * run draws nothing rather than a placeholder.
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
      <div className={styles.head} aria-hidden="true">
        <span>Scenario</span>
        <div className={styles.headStages}>
          {STAGES.map((stage) => (
            <span key={stage}>{STAGE_LABEL[stage]} </span>
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
  const label = `${OUTCOME_WORD[journey.outcome]}, reached ${
    STAGE_LABEL[journey.reached] ?? journey.reached
  } of ${String(STAGES.length)} stages`;
  return (
    <li
      className={styles.row}
      data-outcome={journey.outcome}
      style={{ animationDelay: `${String(60 + index * 70)}ms` }}
    >
      <span className={styles.scenario} title={journey.missionKey}>
        {scenarioName(journey.missionKey)}
      </span>
      <div className={styles.track} role="img" aria-label={label}>
        {STAGES.map((stage, position) => (
          <span
            key={stage}
            className={styles.cell}
            data-state={
              position < reached - 1 ? "passed" : position === reached - 1 ? "final" : "unreached"
            }
          />
        ))}
      </div>
      <span className={styles.outcome}>{OUTCOME_WORD[journey.outcome]}</span>
      {journey.stoppedBecause === null ? null : (
        <p className={styles.reason}>{journey.stoppedBecause}</p>
      )}
    </li>
  );
}
