"use client";

import styles from "@/components/console.module.css";

/**
 * Segment level error boundary. The message stays a sentence; the error object itself is
 * not rendered, so no stack or internal detail reaches the screen.
 */
export default function ConsoleError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className={styles.errorBox} role="alert">
      <p className={styles.errorTitle}>This page could not be rendered</p>
      <p>
        The console hit an unexpected error. Retry the page; if it keeps failing, check that the
        AgentRank API is running.
      </p>
      <button className={styles.button} type="button" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
