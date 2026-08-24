import type { ReactNode } from "react";

import { CopyButton } from "./CopyButton";
import styles from "./console.module.css";

/**
 * A disclosure holding the identifiers and digests a merchant or developer needs for
 * reproducibility, kept out of the primary reading path.
 */
export function TechnicalDetails({
  summary = "Technical identifiers",
  children,
}: {
  summary?: string;
  children: ReactNode;
}) {
  return (
    <details className={styles.tech}>
      <summary className={styles.techSummary}>{summary}</summary>
      <div className={styles.techBody}>{children}</div>
    </details>
  );
}

export function IdRow({ label, value }: { label: string; value: string | null }) {
  if (value === null) {
    return (
      <div className={styles.idRow}>
        <span className={styles.idLabel}>{label}</span>
        <span className={styles.cellMuted}>not recorded</span>
      </div>
    );
  }
  return (
    <div className={styles.idRow}>
      <span className={styles.idLabel}>{label}</span>
      <span>{value}</span>
      <CopyButton value={value} label={`Copy ${label.toLowerCase()}`} />
    </div>
  );
}
