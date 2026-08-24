import { StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import { traceEventLabel } from "@/lib/labels";
import type { Tone } from "@/lib/labels";
import type { TraceProjection } from "@/lib/insights/types";

const EVENT_TONES: Record<string, Tone> = {
  MODEL_REQUEST: "info",
  MODEL_RESPONSE: "neutral",
  TOOL_CALL: "neutral",
  TOOL_RESULT: "ok",
  TOOL_ERROR: "warn",
  AGENT_FINAL: "ok",
  AGENT_ABORT: "warn",
  PROVIDER_ERROR: "fail",
};

/**
 * The bounded, ordered trace page for one mission.
 *
 * Events render exactly as the API delivered them: sequence, type, recorded time and the
 * redacted payload verbatim as pretty printed JSON. Nothing is inferred between events,
 * nothing is rendered as HTML or Markdown, and a gap in the sequence is reported rather
 * than hidden, because an untrustworthy ordering would make every reading below it wrong.
 */
export function TraceExplorer({ trace }: { trace: TraceProjection }) {
  const ordered = isOrdered(trace);
  return (
    <div>
      {!ordered ? (
        <p className={styles.warningItem} role="alert">
          The API returned trace events out of sequence order. They are shown as received; treat the
          pairing of calls and results with care.
        </p>
      ) : null}
      <p className={styles.finePrintTight}>
        {`${trace.events.length} of ${String(trace.total_events)} event(s) shown. Payloads are the redacted capture, unchanged.`}
      </p>
      {trace.events.length === 0 ? (
        <div className={styles.panel}>
          <div className={styles.emptyState}>
            <p className={styles.emptyTitle}>No trace events</p>
            <p>
              Missions executed by the deterministic reference executor record no model trace. LLM
              buyer missions do.
            </p>
          </div>
        </div>
      ) : (
        <ol className={styles.traceList}>
          {trace.events.map((event) => {
            const tone = EVENT_TONES[event.event_type] ?? "neutral";
            return (
              <li key={event.sequence} className={styles.traceItem} data-tone={tone}>
                <div className={styles.traceHead}>
                  <span className={styles.mono}>{String(event.sequence)}</span>
                  <StatusMark
                    tone={tone}
                    label={traceEventLabel(event.event_type)}
                    description={`Event type: ${event.event_type}`}
                  />
                  <span className={styles.cellMuted}>{formatTimestamp(event.recorded_at)}</span>
                  {typeof event.payload["tool"] === "string" ? (
                    <span className={styles.mono}>{event.payload["tool"]}</span>
                  ) : null}
                </div>
                <details>
                  <summary className={styles.techSummary}>Payload</summary>
                  <pre className={styles.tracePayload}>
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                </details>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function isOrdered(trace: TraceProjection): boolean {
  for (let index = 1; index < trace.events.length; index += 1) {
    const previous = trace.events[index - 1];
    const current = trace.events[index];
    if (previous === undefined || current === undefined || previous.sequence >= current.sequence) {
      return false;
    }
  }
  return true;
}
