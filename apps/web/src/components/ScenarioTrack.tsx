import styles from "./merchant.module.css";

export interface ScenarioCounts {
  readonly succeeded: number;
  readonly failed: number;
  readonly abstained: number;
  readonly errored: number;
  readonly unfinished: number;
}

/** Above this, one block per scenario stops being readable and the track becomes a bar. */
const DISCRETE_LIMIT = 48;

/**
 * AgentRank's scenario track: one block per shopping scenario, stamped in outcome order.
 *
 * The block is the product's honest unit. A filled green block is a completed purchase, an
 * ochre one a failure, a hollow one a decline, a hatched one never reached an outcome. No
 * percentage and no geometry stands in for a count; at large suite sizes the track falls
 * back to a proportional bar rather than shrinking blocks below legibility. The legend
 * carries every number in text, so nothing here depends on color alone.
 */
export function ScenarioTrack({ counts }: { counts: ScenarioCounts }) {
  const total =
    counts.succeeded + counts.failed + counts.abstained + counts.errored + counts.unfinished;
  if (total === 0) {
    return null;
  }
  const legend = [
    { label: "completed", count: counts.succeeded },
    { label: "declined", count: counts.abstained },
    { label: "failed", count: counts.failed },
    { label: "not measured", count: counts.errored },
    { label: "unfinished", count: counts.unfinished },
  ].filter((entry) => entry.count > 0);
  const summary = legend.map((entry) => `${String(entry.count)} ${entry.label}`).join(", ");

  return (
    <div className={styles.track}>
      {total <= DISCRETE_LIMIT ? (
        <div className={styles.trackRow} role="img" aria-label={`Scenario outcomes: ${summary}`}>
          {blocks(counts).map((outcome, index) => (
            <span
              // The track is a fixed ordering of identical stamps; the index is the identity.
              key={index}
              className={styles.trackBlock}
              data-outcome={outcome}
              style={{ animationDelay: `${String(80 + index * 24)}ms` }}
            />
          ))}
        </div>
      ) : (
        <div className={styles.trackBar} role="img" aria-label={`Scenario outcomes: ${summary}`}>
          {segments(counts, total).map((segment) => (
            <span
              key={segment.outcome}
              className={styles.trackBarSegment}
              data-outcome={segment.outcome}
              style={{ width: `${segment.share.toFixed(2)}%` }}
            />
          ))}
        </div>
      )}
      <p className={styles.trackLegend}>
        {legend.map((entry, index) => (
          <span key={entry.label} className={styles.trackLegendItem}>
            {index > 0 ? <span aria-hidden="true"> / </span> : null}
            <span className={styles.trackLegendCount}>{String(entry.count)}</span> {entry.label}
          </span>
        ))}
      </p>
    </div>
  );
}

type Outcome = "succeeded" | "abstained" | "failed" | "errored" | "unfinished";

function blocks(counts: ScenarioCounts): Outcome[] {
  return [
    ...Array.from({ length: counts.succeeded }, (): Outcome => "succeeded"),
    ...Array.from({ length: counts.abstained }, (): Outcome => "abstained"),
    ...Array.from({ length: counts.failed }, (): Outcome => "failed"),
    ...Array.from({ length: counts.errored }, (): Outcome => "errored"),
    ...Array.from({ length: counts.unfinished }, (): Outcome => "unfinished"),
  ];
}

function segments(counts: ScenarioCounts, total: number) {
  return (
    [
      { outcome: "succeeded", count: counts.succeeded },
      { outcome: "abstained", count: counts.abstained },
      { outcome: "failed", count: counts.failed },
      { outcome: "errored", count: counts.errored },
      { outcome: "unfinished", count: counts.unfinished },
    ] as const
  )
    .filter((segment) => segment.count > 0)
    .map((segment) => ({ outcome: segment.outcome, share: (segment.count / total) * 100 }));
}
