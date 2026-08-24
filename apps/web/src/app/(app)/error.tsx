"use client";

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
    <div
      style={{
        padding: 16,
        border: "1px solid var(--state-fail)",
        borderRadius: 4,
        background: "var(--surface-raised)",
      }}
      role="alert"
    >
      <p style={{ margin: "0 0 4px", fontWeight: 600, color: "var(--state-fail)" }}>
        This page could not be rendered
      </p>
      <p style={{ margin: "0 0 8px", fontSize: 12, color: "var(--text-muted)" }}>
        The console hit an unexpected error. Retry the page; if it keeps failing, check that the
        AgentRank API is running.
      </p>
      <button
        type="button"
        onClick={reset}
        style={{
          border: "1px solid var(--border-strong)",
          borderRadius: 3,
          background: "var(--surface)",
          padding: "6px 14px",
          cursor: "pointer",
          font: "inherit",
          fontSize: 13,
        }}
      >
        Try again
      </button>
    </div>
  );
}
