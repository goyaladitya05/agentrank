import { DeltaTable } from "@/components/Demand";
import {
  EmptyState,
  KeyValueList,
  Panel,
  Section,
  StatusMark,
  WarningList,
} from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import { formatRate } from "@/lib/format";
import {
  conclusionKindLabel,
  designationLabel,
  statusLabel,
  transitionDirectionLabel,
} from "@/lib/labels";
import { armByLabel, outcomeDeltaRows, pairOrderLabel } from "@/lib/insights/experiment";
import type { ExperimentComparison } from "@/lib/insights/types";

/**
 * The controlled raw versus compiled comparison.
 *
 * Methodology warnings sit at the top because they qualify every number below them. The
 * conclusion is quoted from the backend verbatim: parity stays parity, differences stay
 * observations, and neither is ever restyled into a win.
 */
export function ExperimentDetailContent({ data }: { data: ExperimentComparison }) {
  const conclusion = conclusionKindLabel(data.conclusion.kind);
  const designation = designationLabel(data.benchmark_designation);
  const raw = armByLabel(data.arms, "RAW");
  const compiled = armByLabel(data.arms, "COMPILED");

  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Controlled experiment</h1>
        <StatusMark
          tone={conclusion.tone}
          label={conclusion.label}
          description={`Backend conclusion kind: ${data.conclusion.kind}`}
        />
        <StatusMark
          tone={designation.tone}
          label={designation.label}
          description={designation.note}
        />
      </div>

      <Section title="Methodology warnings" hint="These qualify every number below.">
        <WarningList warnings={data.warnings} />
        {data.warnings.length === 0 ? (
          <Panel>
            <p>No methodology warning was raised for this experiment.</p>
          </Panel>
        ) : null}
        <KeyValueList
          entries={[
            {
              term: "Sample pairs",
              value: `${String(data.completed_sample_pairs)} completed of ${String(
                data.declared_sample_pairs,
              )} declared`,
            },
            { term: "Pair order", value: pairOrderLabel(data.pair_order) },
          ]}
        />
      </Section>

      <Section title="Conclusion">
        <Panel>
          <blockquote className={styles.quote}>
            &ldquo;{data.conclusion.statement}&rdquo;
          </blockquote>
        </Panel>
      </Section>

      <Section title="Experiment identity">
        <Panel>
          <KeyValueList
            entries={[
              { term: "Designation", value: designation.label },
              { term: "Pair order", value: pairOrderLabel(data.pair_order) },
            ]}
          />
          <TechnicalDetails summary="Experiment identifiers">
            <IdRow label="Experiment id" value={data.experiment_id} />
            <IdRow label="Buyer configuration" value={data.buyer_configuration_digest} />
            <IdRow label="Engine identity" value={data.engine_identity} />
          </TechnicalDetails>
        </Panel>
      </Section>

      <Section title="Primary outcomes" hint="Raw arm versus compiled arm.">
        <ArmsTable raw={raw} compiled={compiled} />
        <OutcomeDeltas raw={raw} compiled={compiled} />
      </Section>

      <Section title="Simulated demand deltas">
        <DeltaTable deltas={data.demand_delta_by_currency} />
      </Section>

      <MissionTransitions data={data} />
    </>
  );
}

function ArmsTable({
  raw,
  compiled,
}: {
  raw: ExperimentComparison["arms"][number] | null;
  compiled: ExperimentComparison["arms"][number] | null;
}) {
  if (raw === null || compiled === null) {
    return (
      <div className={styles.panel}>
        <EmptyState
          title="No completed arms"
          explanation="An arm appears once at least one of its samples has finished."
        />
      </div>
    );
  }
  return (
    <div className={styles.tableScroll}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Arm</th>
            <th scope="col" className={styles.num}>
              Samples (done/planned)
            </th>
            <th scope="col" className={styles.num}>
              Completion mean
            </th>
            <th scope="col" className={styles.num}>
              Succeeded / failed / abstained
            </th>
            <th scope="col">Safety</th>
            <th scope="col" className={styles.num}>
              Provider outages
            </th>
            <th scope="col" className={styles.num}>
              Model invocations
            </th>
            <th scope="col" className={styles.num}>
              Tool calls
            </th>
            <th scope="col">Resolved models</th>
          </tr>
        </thead>
        <tbody>
          {[raw, compiled].map((arm) => (
            <tr key={arm.arm}>
              <td>{arm.arm === "RAW" ? "Raw (storefront view)" : "Compiled (agent-ready view)"}</td>
              <td className={styles.num}>
                {`${String(arm.completed_samples)} / ${String(arm.planned_samples)}`}
              </td>
              <td className={styles.num}>{formatRate(arm.completion_rate_mean)}</td>
              <td className={styles.num}>
                {arm.metrics_totals === null
                  ? "not recorded"
                  : `${String(arm.metrics_totals.missions_succeeded)} / ${String(
                      arm.metrics_totals.missions_failed,
                    )} / ${String(arm.metrics_totals.missions_abstained)}`}
              </td>
              <td>
                {arm.metrics_totals === null
                  ? "not recorded"
                  : safetyCell(
                      arm.metrics_totals.unsafe_attempts,
                      arm.metrics_totals.unsafe_completions,
                    )}
              </td>
              <td className={styles.num}>{String(arm.terminated_provider_outages)}</td>
              <td className={styles.num}>{String(arm.model_invocations)}</td>
              <td className={styles.num}>{String(arm.tool_calls)}</td>
              <td className={styles.mono}>
                {arm.resolved_models.length === 0 ? "not reported" : arm.resolved_models.join(", ")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function safetyCell(attempts: number, escapes: number): string {
  if (escapes > 0) {
    return `${String(escapes)} escape(s), ${String(attempts)} attempt(s)`;
  }
  if (attempts > 0) {
    return `${String(attempts)} attempt(s), all blocked`;
  }
  return "No unsafe attempts";
}

function OutcomeDeltas({
  raw,
  compiled,
}: {
  raw: ExperimentComparison["arms"][number] | null;
  compiled: ExperimentComparison["arms"][number] | null;
}) {
  const rows = outcomeDeltaRows(raw?.metrics_totals ?? null, compiled?.metrics_totals ?? null);
  if (rows.length === 0) {
    return (
      <p className={styles.finePrintTight}>
        Outcome totals need completed samples in both arms; none are comparable yet.
      </p>
    );
  }
  return (
    <div className={styles.tableScroll}>
      <table className={styles.table}>
        <caption
          className={styles.cellMuted}
          style={{ textAlign: "left", padding: "8px 12px 4px" }}
        >
          Compiled totals minus raw totals across completed samples.
        </caption>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col" className={styles.num}>
              Raw
            </th>
            <th scope="col" className={styles.num}>
              Compiled
            </th>
            <th scope="col" className={styles.num}>
              Change
            </th>
            <th scope="col">What a change means</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.metric}>
              <td>{row.metric}</td>
              <td className={styles.num}>{row.raw}</td>
              <td className={styles.num}>{row.compiled}</td>
              <td className={styles.num + " " + styles.mono}>{row.change}</td>
              <td>{row.note ?? <span className={styles.cellMuted}>-</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MissionTransitions({ data }: { data: ExperimentComparison }) {
  if (data.mission_transitions.length === 0) {
    return (
      <Section title="Mission transitions">
        <div className={styles.panel}>
          <EmptyState
            title="No mission transitions"
            explanation={
              data.conclusion.kind === "PARITY"
                ? "Every completed pair agreed on every mission's outcome, which is what parity means here."
                : "Every mission kept the same terminal position in both arms."
            }
          />
        </div>
      </Section>
    );
  }
  return (
    <Section
      title="Mission transitions"
      hint="Missions whose terminal position differed between arms."
    >
      <div className={styles.tableScroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Pair</th>
              <th scope="col">Mission</th>
              <th scope="col">Raw arm</th>
              <th scope="col">Compiled arm</th>
              <th scope="col">Direction</th>
            </tr>
          </thead>
          <tbody>
            {data.mission_transitions.map((transition) => {
              const direction = transitionDirectionLabel(transition.direction);
              const rawStatus = statusLabel(transition.raw_status);
              const compiledStatus = statusLabel(transition.compiled_status);
              return (
                <tr key={`${transition.pair_ordinal}:${transition.mission_key}`}>
                  <td className={styles.num}>{String(transition.pair_ordinal)}</td>
                  <td className={styles.mono}>{transition.mission_key}</td>
                  <td>
                    <StatusMark tone={rawStatus.tone} label={rawStatus.label} />
                    {transition.raw_primary_failure_reason !== null ? (
                      <span className={styles.cellMuted}>
                        {" "}
                        {transition.raw_primary_failure_reason}
                      </span>
                    ) : null}
                  </td>
                  <td>
                    <StatusMark tone={compiledStatus.tone} label={compiledStatus.label} />
                    {transition.compiled_primary_failure_reason !== null ? (
                      <span className={styles.cellMuted}>
                        {" "}
                        {transition.compiled_primary_failure_reason}
                      </span>
                    ) : null}
                  </td>
                  <td>
                    <StatusMark
                      tone={direction.tone}
                      label={direction.label}
                      description={`Direction: ${transition.direction}`}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
