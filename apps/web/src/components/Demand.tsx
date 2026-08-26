import { formatMoney, formatSignedMoney } from "@/lib/format";
import type { SimulatedDemandBucket } from "@/lib/insights/types";

import { EmptyState } from "./Primitives";
import styles from "./console.module.css";

/**
 * Simulated benchmark demand, one row group per currency.
 *
 * Two rules are structural here: every column says simulated, and values in different
 * currencies are always different rows and never summed or converted.
 */
export function DemandTable({
  buckets,
  caption,
}: {
  buckets: readonly SimulatedDemandBucket[];
  caption: string;
}) {
  if (buckets.length === 0) {
    return (
      <div className={styles.panel}>
        <EmptyState
          title="No simulated demand recorded"
          explanation="Demand figures appear once a run completes missions that carry simulated purchase value."
        />
      </div>
    );
  }

  const ordered = [...buckets].sort((a, b) => a.currency.localeCompare(b.currency));

  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Simulated demand by currency">
      <table className={styles.table}>
        <caption className={styles.cellMuted} style={{ textAlign: "left", padding: "4px 12px" }}>
          {caption}
        </caption>
        <thead>
          <tr>
            <th scope="col">Currency</th>
            <th scope="col" className={styles.num}>
              Simulated potential
            </th>
            <th scope="col" className={styles.num}>
              Simulated captured
            </th>
            <th scope="col" className={styles.num}>
              Simulated lost
            </th>
            <th scope="col" className={styles.num}>
              Simulated unmeasured
            </th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((bucket) => (
            <tr key={bucket.currency}>
              <td className={styles.mono}>{bucket.currency}</td>
              <td className={styles.num}>
                {formatMoney(bucket.simulated_potential_demand_amount_minor, bucket.currency)}
              </td>
              <td className={styles.num}>
                {formatMoney(bucket.simulated_captured_demand_amount_minor, bucket.currency)}
              </td>
              <td className={styles.num}>
                {formatMoney(bucket.simulated_lost_demand_amount_minor, bucket.currency)}
              </td>
              <td className={styles.num}>
                {formatMoney(bucket.simulated_not_measured_demand_amount_minor, bucket.currency)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Signed per currency deltas for an experiment comparison. Never summed across rows. */
export function DeltaTable({
  deltas,
}: {
  deltas: readonly {
    currency: string;
    simulated_potential_delta_amount_minor: number;
    simulated_captured_delta_amount_minor: number;
    simulated_lost_delta_amount_minor: number;
    simulated_not_measured_delta_amount_minor: number;
  }[];
}) {
  if (deltas.length === 0) {
    return <p className={styles.finePrint}>No simulated demand was compared.</p>;
  }
  return (
    <div
      className={styles.tableScroll}
      tabIndex={0}
      aria-label="Simulated demand compared between arms"
    >
      <table className={styles.table}>
        <caption className={styles.cellMuted} style={{ textAlign: "left", padding: "4px 12px" }}>
          Compiled arm minus raw arm. Positive means the compiled arm carried more.
        </caption>
        <thead>
          <tr>
            <th scope="col">Currency</th>
            <th scope="col" className={styles.num}>
              Simulated potential delta
            </th>
            <th scope="col" className={styles.num}>
              Simulated captured delta
            </th>
            <th scope="col" className={styles.num}>
              Simulated lost delta
            </th>
            <th scope="col" className={styles.num}>
              Simulated unmeasured delta
            </th>
          </tr>
        </thead>
        <tbody>
          {deltas.map((delta) => (
            <tr key={delta.currency}>
              <td className={styles.mono}>{delta.currency}</td>
              <td className={styles.num}>
                {formatSignedMoney(delta.simulated_potential_delta_amount_minor, delta.currency)}
              </td>
              <td className={styles.num}>
                {formatSignedMoney(delta.simulated_captured_delta_amount_minor, delta.currency)}
              </td>
              <td className={styles.num}>
                {formatSignedMoney(delta.simulated_lost_delta_amount_minor, delta.currency)}
              </td>
              <td className={styles.num}>
                {formatSignedMoney(delta.simulated_not_measured_delta_amount_minor, delta.currency)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
