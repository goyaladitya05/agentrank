"use client";

import { useState } from "react";

import styles from "@/components/console.module.css";

export function PublishRepresentation({
  runId,
  sourceLabel,
  action,
}: {
  runId: string;
  sourceLabel: string;
  action: (formData: FormData) => void | Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <button className={styles.button} type="button" onClick={() => setConfirming(true)}>
        Review publication
      </button>
    );
  }
  return (
    <form action={action} aria-label="Confirm representation publication">
      <p>
        Publish the immutable representation for source {sourceLabel} from compiler run {runId}?
        This does not rerun a benchmark.
      </p>
      <button className={styles.button} type="submit">
        Publish representation
      </button>{" "}
      <button className={styles.textLink} type="button" onClick={() => setConfirming(false)}>
        Cancel
      </button>
    </form>
  );
}
