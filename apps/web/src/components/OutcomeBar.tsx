import { statusLabel } from "@/lib/labels";

import styles from "./console.module.css";

interface OutcomeCounts {
  readonly succeeded: number;
  readonly failed: number;
  readonly abstained: number;
  readonly errored: number;
  readonly unfinished: number;
}

/**
 * Mission outcome distribution as one segmented bar plus its numbers in text.
 *
 * The bar is decorative scanning support; the legend carries the actual values, so the
 * distribution never depends on color or geometry to be read.
 */
export function OutcomeBar({ counts }: { counts: OutcomeCounts }) {
  const segments = [
    { outcome: "SUCCEEDED", count: counts.succeeded },
    { outcome: "ABSTAINED", count: counts.abstained },
    { outcome: "FAILED", count: counts.failed },
    { outcome: "ERRORED", count: counts.errored },
    { outcome: "PENDING", count: counts.unfinished },
  ].filter((segment) => segment.count > 0);
  const total =
    counts.succeeded + counts.failed + counts.abstained + counts.errored + counts.unfinished;

  if (total === 0 || segments.length === 0) {
    return null;
  }

  return (
    <div>
      <div
        className={styles.bar}
        role="img"
        aria-label={segments
          .map((segment) => `${segment.count} ${statusLabel(segment.outcome).label}`)
          .join(", ")}
      >
        {segments.map((segment) => (
          <div
            key={segment.outcome}
            className={styles.barSegment}
            data-outcome={segment.outcome}
            style={{ width: `${((segment.count / total) * 100).toFixed(2)}%` }}
          />
        ))}
      </div>
      <p className={styles.barLegend}>
        {segments.map((segment, index) => (
          <span key={segment.outcome}>
            {index > 0 ? "· " : ""}
            {statusLabel(segment.outcome).label}: {String(segment.count)}
          </span>
        ))}
      </p>
    </div>
  );
}
