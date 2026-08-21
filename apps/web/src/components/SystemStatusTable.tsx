import type { ComponentState, ComponentStatus } from "@/lib/systemStatus";

import styles from "./SystemStatusTable.module.css";

const STATE_LABEL: Record<ComponentState, string> = {
  connected: "Connected",
  unavailable: "Unavailable",
  unknown: "Unknown",
};

export function SystemStatusTable({ components }: { components: readonly ComponentStatus[] }) {
  return (
    <table className={styles.table}>
      <caption>System status</caption>
      <thead>
        <tr>
          <th scope="col">Component</th>
          <th scope="col">State</th>
          <th scope="col">Detail</th>
        </tr>
      </thead>
      <tbody>
        {components.map((component) => (
          <tr key={component.name}>
            <th scope="row" className={styles.component}>
              {component.name}
            </th>
            <td>
              {/* The state drives styling through a data attribute rather than a looked
                  up class name, so the stylesheet stays the single place that decides
                  what each state looks like. */}
              <span className={styles.state} data-state={component.state}>
                <span className={styles.marker} aria-hidden="true" />
                {STATE_LABEL[component.state]}
              </span>
            </td>
            <td className={styles.detail}>{component.detail ?? "none"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
