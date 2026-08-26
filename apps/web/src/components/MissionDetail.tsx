import Link from "next/link";

import { CompilerActions } from "@/components/Findings";
import { TraceExplorer } from "@/components/Trace";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import { formatCount, formatMoney } from "@/lib/format";
import {
  actionabilityLabel,
  actionabilityTone,
  demandBucketLabel,
  evidenceLevelLabel,
  ownerLabel,
  severityLabel,
  severityTone,
} from "@/lib/labels";
import { statusLabel } from "@/lib/labels";
import type { MissionDiagnosis } from "@/lib/insights/types";
import type { TraceProjection } from "@/lib/insights/types";

const TRACE_PAGE_DEFAULT = 100;
const TRACE_PAGE_MAX = 500;

export interface TracePageParams {
  readonly limit: number;
  readonly offset: number;
}

export function clampTracePage(
  limit: string | undefined,
  offset: string | undefined,
): TracePageParams {
  const parsedLimit = Number.parseInt(limit ?? "", 10);
  const parsedOffset = Number.parseInt(offset ?? "", 10);
  const safeLimit = Number.isFinite(parsedLimit)
    ? Math.min(Math.max(parsedLimit, 1), TRACE_PAGE_MAX)
    : TRACE_PAGE_DEFAULT;
  const safeOffset = Number.isFinite(parsedOffset) ? Math.max(parsedOffset, 0) : 0;
  return { limit: safeLimit, offset: safeOffset };
}

/**
 * The forensic screen for one mission.
 *
 * One screen answers what happened, who owns it, what the merchant can do, what the
 * mission touched in the commerce runtime, and exactly which evidence says so. The trace
 * stays a bounded page below the reading, not a replacement for it.
 */
export function MissionDetailContent({
  diagnosis,
  trace,
  tracePage,
}: {
  diagnosis: MissionDiagnosis;
  trace: TraceProjection | null;
  tracePage: TracePageParams | null;
}) {
  const status = statusLabel(diagnosis.status);
  const primary = diagnosis.findings.find((finding) => finding.code === diagnosis.primary_code);
  const secondary = diagnosis.findings.filter((finding) => finding.code !== diagnosis.primary_code);
  const allEvidence = diagnosis.findings.flatMap((finding) => finding.evidence);
  const seen = new Set<string>();
  const commerceEvidence = allEvidence.filter((reference) => {
    if (!["checkout", "payment_attempt", "variant"].includes(reference.kind)) {
      return false;
    }
    const key = `${reference.kind}:${reference.identifier}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
  const uniqueEvidence = allEvidence.filter((reference) => {
    const key = `${reference.kind}:${reference.identifier}:${reference.establishes}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });

  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>
          <span className={styles.mono}>{diagnosis.mission_key}</span>
        </h1>
        <StatusMark
          tone={status.tone}
          label={status.label}
          description={`Mission status: ${diagnosis.status}`}
        />
        <Link
          className={styles.textLink}
          href={`/lab/runs/${encodeURIComponent(diagnosis.run_id)}`}
        >
          Back to run
        </Link>
      </div>

      <Section title="Mission">
        <Panel>
          <KeyValueList
            entries={[
              { term: "What happened", value: diagnosis.outcome },
              {
                term: "Simulated demand",
                value:
                  diagnosis.simulated_demand.length === 0
                    ? "none attributed"
                    : diagnosis.simulated_demand
                        .map(
                          (effect) =>
                            `${demandBucketLabel(effect.bucket)}: ${formatMoney(
                              effect.simulated_amount_minor,
                              effect.currency,
                            )}`,
                        )
                        .join(" · "),
              },
              {
                term: "Interaction cost",
                value: interactionsText(diagnosis),
              },
            ]}
          />
          <TechnicalDetails summary="Diagnostic identifiers">
            <IdRow label="Mission run id" value={diagnosis.mission_run_id} />
            <IdRow label="Run id" value={diagnosis.run_id} />
            <IdRow label="Engine identity" value={diagnosis.engine_identity} />
          </TechnicalDetails>
        </Panel>
      </Section>

      <Section title="Diagnosis">
        <div className={styles.panel}>
          {primary === undefined ? (
            <div className={styles.finding}>
              <p>No primary diagnosis. This mission produced no grouped finding.</p>
            </div>
          ) : (
            <FindingBlock finding={primary} lead />
          )}
          {secondary.map((finding) => (
            <FindingBlock key={finding.code} finding={finding} />
          ))}
        </div>
      </Section>

      <Section title="Commerce result">
        {commerceEvidence.length === 0 ? (
          <Panel>
            <EmptyState
              title="No commerce artifacts"
              explanation="This mission did not reach a quote, a payment attempt or a selected variant."
            />
          </Panel>
        ) : (
          <div className={styles.tableScroll} tabIndex={0} aria-label="Commerce artifacts">
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Artifact</th>
                  <th scope="col">Identifier</th>
                  <th scope="col">Establishes</th>
                </tr>
              </thead>
              <tbody>
                {commerceEvidence.map((reference) => (
                  <tr key={`${reference.kind}:${reference.identifier}`}>
                    <td>{artifactLabel(reference.kind)}</td>
                    <td className={styles.mono}>{reference.identifier}</td>
                    <td>{reference.establishes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Evidence">
        {uniqueEvidence.length === 0 ? (
          <Panel>
            <EmptyState
              title="No evidence references"
              explanation="The diagnosis rests on the run's own records and no deeper artifact was cited."
            />
          </Panel>
        ) : (
          <div className={styles.tableScroll} tabIndex={0} aria-label="Evidence references">
            <table className={styles.table}>
              <thead>
                <tr>
                  <th scope="col">Kind</th>
                  <th scope="col">Identifier</th>
                  <th scope="col">Establishes</th>
                </tr>
              </thead>
              <tbody>
                {uniqueEvidence.map((reference) => (
                  <tr key={`${reference.kind}:${reference.identifier}:${reference.establishes}`}>
                    <td className={styles.mono}>{reference.kind}</td>
                    <td className={styles.mono}>{reference.identifier}</td>
                    <td>{reference.establishes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Trace" hint={tracePageHint(tracePage) ?? ""}>
        {trace === null ? (
          <Panel>
            <p>Trace not loaded.</p>
          </Panel>
        ) : (
          <>
            <TracePager
              runId={diagnosis.run_id}
              missionRunId={diagnosis.mission_run_id}
              trace={trace}
              params={tracePage}
            />
            <TraceExplorer trace={trace} />
          </>
        )}
      </Section>
    </>
  );
}

function FindingBlock({
  finding,
  lead = false,
}: {
  finding: MissionDiagnosis["findings"][number];
  lead?: boolean;
}) {
  return (
    <article className={styles.finding}>
      <div className={styles.findingTop}>
        <StatusMark
          tone={severityTone(finding.severity)}
          label={severityLabel(finding.severity)}
          description={`Diagnostic code ${finding.code}`}
        />
        <h3 className={styles.findingTitle}>{lead ? finding.summary : `${finding.summary}`}</h3>
      </div>
      <div className={styles.findingMeta}>
        <span>
          Owner: <strong>{ownerLabel(finding.owner)}</strong>
        </span>
        <StatusMark
          tone={actionabilityTone(finding.actionability)}
          label={actionabilityLabel(finding.actionability)}
          description={`Actionability: ${finding.actionability}`}
        />
        <span>Evidence: {evidenceLevelLabel(finding.evidence_level)}</span>
        {finding.attribute_keys.length > 0 ? (
          <span>
            Attributes: <span className={styles.mono}>{finding.attribute_keys.join(", ")}</span>
          </span>
        ) : null}
      </div>
      {finding.recommendation !== null ? (
        <p className={styles.findingRecommendation}>{finding.recommendation}</p>
      ) : null}
      <CompilerActions references={finding.compiler_references} />
    </article>
  );
}

function interactionsText(diagnosis: MissionDiagnosis): string {
  if (diagnosis.model_invocations === null && diagnosis.tool_calls === null) {
    return "Not reported (no model trace)";
  }
  return [
    formatCount(diagnosis.model_invocations, "provider round trip"),
    formatCount(diagnosis.tool_calls, "tool call"),
    formatCount(diagnosis.tool_errors, "tool error"),
  ].join(" · ");
}

function artifactLabel(kind: string): string {
  switch (kind) {
    case "checkout":
      return "Checkout";
    case "payment_attempt":
      return "Payment attempt";
    case "variant":
      return "Variant";
    default:
      return kind;
  }
}

function tracePageHint(params: TracePageParams | null): string | undefined {
  if (params === null) {
    return undefined;
  }
  return `Page of ${String(params.limit)} events`;
}

function TracePager({
  runId,
  missionRunId,
  trace,
  params,
}: {
  runId: string;
  missionRunId: string;
  trace: TraceProjection;
  params: TracePageParams | null;
}) {
  if (params === null || trace.total_events <= params.limit) {
    return null;
  }
  const base = `/lab/runs/${encodeURIComponent(runId)}/missions/${encodeURIComponent(missionRunId)}`;
  const withParams = (offset: number) =>
    `${base}?limit=${String(params.limit)}&offset=${String(offset)}`;
  const previousOffset = Math.max(params.offset - params.limit, 0);
  const nextOffset = params.offset + params.limit;
  const hasPrevious = params.offset > 0;
  const hasNext = nextOffset < trace.total_events;
  return (
    <p style={{ display: "flex", gap: 16 }}>
      {hasPrevious ? (
        <Link className={styles.textLink} href={withParams(previousOffset)}>
          Previous
        </Link>
      ) : (
        <span className={styles.cellMuted}>Previous</span>
      )}
      {hasNext ? (
        <Link className={styles.textLink} href={withParams(nextOffset)}>
          Next
        </Link>
      ) : (
        <span className={styles.cellMuted}>Next</span>
      )}
    </p>
  );
}
