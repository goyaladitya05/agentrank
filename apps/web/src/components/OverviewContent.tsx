import Link from "next/link";

import { DemandTable } from "@/components/Demand";
import { Panel, Section, StatusMark } from "@/components/Primitives";
import { ScenarioJourneys } from "@/components/ScenarioJourneys";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { formatMoney, formatTimestamp } from "@/lib/format";
import { conclusionKindLabel, severityLabel, severityTone } from "@/lib/labels";
import { providerSentence, safetyReading } from "@/lib/insights/summary";
import { groupFindings, latestFinishedRun, nextAction } from "@/lib/insights/merchant";
import type { NextAction } from "@/lib/insights/merchant";
import type { EvaluationPreflight } from "@/lib/evaluation";
import { scenarioJourneys } from "@/lib/insights/journey";
import type {
  MerchantFinding,
  MerchantOverview,
  RunDiagnostics,
  RunSummary,
} from "@/lib/insights/types";

/**
 * The merchant overview, composed as one editorial surface: the verdict set large, the
 * scenario track under it, one row of figures between hairlines, and the next step as a
 * full-width band under the strong rule. No number here is invented: every count is the
 * API's, the demand figures always say simulated, and an aborted run is labelled before
 * its numbers.
 *
 * The preflight is the server's answer about what this merchant can launch and whether one
 * is pending; it may be null when that read failed, and the page still renders everything
 * the overview insight carries.
 */
export function OverviewContent({
  data,
  preflight,
  run: diagnostics = null,
}: {
  data: MerchantOverview;
  preflight: EvaluationPreflight | null;
  /** The latest finished run's missions, for the journey board. Null when unread. */
  run?: RunDiagnostics | null;
}) {
  const run = latestFinishedRun(data.runs);
  const action = nextAction(data, preflight);

  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Overview</h1>
        <TechnicalDetails summary="Technical identifiers">
          <IdRow label="Engine identity" value={data.engine_identity} />
          <IdRow label="Merchant id" value={data.merchant_id} />
        </TechnicalDetails>
      </div>

      {run === null ? (
        <NewMerchantMasthead action={action} />
      ) : (
        <MeasuredMasthead run={run} findings={data.top_findings} />
      )}

      <NextStepSlip action={action} />

      {run !== null && diagnostics !== null ? (
        <Section
          index="01"
          title="What the agents did"
          hint="One row per scenario, from what the run recorded."
        >
          <ScenarioJourneys
            journeys={scenarioJourneys(diagnostics.missions)}
            caption="A journey stops at the stage AgentRank's trusted records say it stopped at. Nothing here is a model's own account of itself."
          />
        </Section>
      ) : null}

      {run !== null ? <AttentionSection data={data} run={run} /> : null}

      {run !== null ? (
        <Section
          index="03"
          title="Simulated demand"
          hint="Simulated benchmark figures across recent evaluations. Not revenue."
        >
          <DemandTable
            buckets={data.simulated_demand_totals_by_currency}
            caption="Totals across your recent evaluations, one row per currency."
          />
        </Section>
      ) : null}

      <ExperimentNote data={data} />

      {run !== null ? (
        <p className={styles.finePrint}>
          Every evaluation and change is in{" "}
          <Link className={styles.rowLink} href="/history">
            your history
          </Link>
          . Full technical detail, including runs, traces and methodology, is in{" "}
          <Link className={styles.rowLink} href="/lab">
            AgentRank Lab
          </Link>
          .
        </p>
      ) : null}
    </>
  );
}

/** The four steps a new merchant walks, with the current one decided by the next action. */
const JOURNEY = [
  { step: "Import your store", kinds: ["import-store"] },
  { step: "Prepare the evaluation", kinds: ["prepare-evaluation"] },
  { step: "Run the evaluation", kinds: ["run-first-evaluation", "evaluation-in-progress"] },
  { step: "See your results", kinds: [] },
] as const;

function NewMerchantMasthead({ action }: { action: NextAction }) {
  const currentIndex = JOURNEY.findIndex((entry) =>
    (entry.kinds as readonly string[]).includes(action.kind),
  );
  return (
    <header className={merchant.masthead}>
      <p className={merchant.eyebrow}>AI shopping performance</p>
      <h2 className={merchant.mastStatement}>Can AI shopping agents buy from your store?</h2>
      <p className={merchant.mastReading}>
        AgentRank sends shopping agents through realistic purchase scenarios against your store,
        shows you exactly where they fail, and separates what you can fix from what is not your
        problem.
      </p>
      <ol className={merchant.journey} aria-label="Setup steps">
        {JOURNEY.map((entry, index) => (
          <li
            key={entry.step}
            className={merchant.journeyStep}
            data-state={
              index === currentIndex ? "current" : index < currentIndex ? "done" : undefined
            }
          >
            <span className={merchant.journeyIndex}>0{index + 1}</span>
            {entry.step}
            {index === currentIndex ? (
              <span className={merchant.journeyNow}>you are here</span>
            ) : null}
          </li>
        ))}
      </ol>
    </header>
  );
}

function MeasuredMasthead({
  run,
  findings,
}: {
  run: RunSummary;
  findings: readonly MerchantFinding[];
}) {
  const grouped = groupFindings(findings);
  const merchantScenarios = distinctMissionCount(grouped.needsAttention);
  return (
    <header className={merchant.masthead}>
      <p className={merchant.eyebrow}>
        AI shopping performance · {run.status === "ABORTED" ? "stopped" : "measured"}{" "}
        {formatTimestamp(run.completed_at)}
      </p>
      <h2 className={merchant.mastStatement}>
        <strong>{String(run.missions_succeeded)}</strong> of{" "}
        <strong>{String(run.purchase_missions)}</strong> purchase scenarios completed
      </h2>
      <p className={merchant.mastReading}>
        {heroSentence(run, merchantScenarios)}{" "}
        {run.status === "ABORTED"
          ? "This evaluation stopped before it finished, so these numbers describe only the scenarios that executed."
          : ""}
      </p>
      <div className={merchant.statRow}>
        <div className={merchant.stat}>
          <span className={merchant.statValue}>{String(run.missions_total)}</span>
          <span className={merchant.statLabel}>Scenarios tested</span>
        </div>
        <div className={merchant.stat}>
          <span className={merchant.statValue} data-tone="ok">
            {String(run.missions_succeeded)}
          </span>
          <span className={merchant.statLabel}>Successful purchases</span>
        </div>
        <div className={merchant.stat}>
          <span
            className={merchant.statValue}
            data-tone={merchantScenarios > 0 ? "warn" : undefined}
          >
            {String(merchantScenarios)}
          </span>
          <span className={merchant.statLabel}>Need your attention</span>
        </div>
        <div className={merchant.stat}>
          <span className={merchant.statValue}>{String(run.provider_failure_missions)}</span>
          <span className={merchant.statLabel}>Provider or system failures</span>
        </div>
        <div className={merchant.stat}>
          <span className={merchant.statValue}>
            {String(run.correct_abstentions)} of {String(run.control_missions)}
          </span>
          <span className={merchant.statLabel}>Correct declines</span>
        </div>
        <CapturedDemandStat run={run} />
      </div>
    </header>
  );
}

/**
 * How many distinct scenarios the merchant-attention findings touched. A count of
 * evidence, not a claim: two findings on the same mission are one scenario.
 */
function distinctMissionCount(findings: readonly MerchantFinding[]): number {
  const missions = new Set<string>();
  for (const finding of findings) {
    for (const missionRunId of finding.mission_run_ids) {
      missions.add(missionRunId);
    }
  }
  return missions.size;
}

function heroSentence(run: RunSummary, merchantScenarios: number): string {
  const parts: string[] = [];
  if (merchantScenarios > 0) {
    parts.push(
      merchantScenarios === 1
        ? "1 scenario needs your attention"
        : `${String(merchantScenarios)} scenarios need your attention`,
    );
  }
  if (run.provider_failure_missions > 0) {
    parts.push(
      `${String(run.provider_failure_missions)} ended on provider failures that need nothing from you`,
    );
  }
  if (parts.length === 0) {
    return run.missions_failed > 0
      ? "The remaining scenarios did not produce merchant-fixable findings."
      : "No scenario failed for a reason that needs you.";
  }
  return `${parts.join("; ")}.`;
}

function CapturedDemandStat({ run }: { run: RunSummary }) {
  if (run.simulated_demand.length === 0) {
    return null;
  }
  return (
    <div className={merchant.stat}>
      <span className={merchant.statValue}>
        {run.simulated_demand
          .map((bucket) =>
            formatMoney(bucket.simulated_captured_demand_amount_minor, bucket.currency),
          )
          .join(" ")}
      </span>
      <span className={merchant.statLabel}>Simulated demand captured</span>
    </div>
  );
}

/** The one action the page leads to, as a band under the strong rule. */
function NextStepSlip({ action }: { action: NextAction }) {
  return (
    <aside className={merchant.slip} aria-label="Next step">
      <div className={merchant.slipText}>
        <p className={merchant.slipEyebrow}>Next step</p>
        <h2 className={merchant.slipTitle}>{action.title}</h2>
        <p className={merchant.slipBody}>{action.body}</p>
      </div>
      <Link className={merchant.primaryButton} href={action.href}>
        {action.label}
      </Link>
    </aside>
  );
}

function AttentionSection({ data, run }: { data: MerchantOverview; run: RunSummary }) {
  const grouped = groupFindings(data.top_findings);
  const safety = safetyReading(run.unsafe_attempts, run.unsafe_completions, 0);
  const providerNote = providerSentence(run.provider_failure_missions);
  return (
    <Section
      index="02"
      title="What needs attention"
      actions={
        grouped.needsAttention.length > 0 ? (
          <Link className={styles.textLink} href="/issues">
            All issues
          </Link>
        ) : undefined
      }
    >
      {grouped.needsAttention.length === 0 ? (
        <div className={styles.emptyState}>
          <p className={styles.emptyTitle}>Nothing needs your attention</p>
          <p>
            {run.status === "ABORTED"
              ? "No merchant-fixable finding came out of the part of this evaluation that executed."
              : "Your latest evaluation produced no finding that requires action from you."}
          </p>
        </div>
      ) : (
        <div className={merchant.entryList}>
          {grouped.needsAttention.slice(0, 5).map((finding) => (
            <article key={finding.key} className={merchant.entry}>
              <div className={merchant.entryGutter}>
                <StatusMark
                  tone={severityTone(finding.severity)}
                  label={severityLabel(finding.severity)}
                />
                <span>
                  {finding.mission_run_ids.length === 1
                    ? "1 scenario"
                    : `${String(finding.mission_run_ids.length)} scenarios`}
                </span>
                {finding.product_ids.length > 0 ? (
                  <span>
                    {finding.product_ids.length === 1
                      ? "1 product"
                      : `${String(finding.product_ids.length)} products`}
                  </span>
                ) : null}
              </div>
              <div>
                <h3 className={merchant.entryTitle}>
                  <Link
                    className={merchant.entryTitleLink}
                    href={`/issues/${encodeURIComponent(finding.key)}`}
                  >
                    {finding.title}
                  </Link>
                </h3>
              </div>
            </article>
          ))}
        </div>
      )}
      {providerNote !== null ? (
        <p className={styles.finePrintTight}>
          <StatusMark tone="neutral" label="Provider" /> {providerNote}
        </p>
      ) : null}
      {safety !== null ? (
        <p className={styles.finePrintTight}>
          <StatusMark tone={safety.tone} label="Safety" /> {safety.text}
        </p>
      ) : null}
    </Section>
  );
}

/**
 * The latest controlled experiment, when one exists. One quoted conclusion and a link into
 * the Lab, where the methodology detail lives. The conclusion is the backend's verbatim;
 * NOT_INTERPRETABLE stays exactly that here.
 */
function ExperimentNote({ data }: { data: MerchantOverview }) {
  const experiment = data.latest_experiment;
  if (experiment === null) {
    return null;
  }
  const conclusion = conclusionKindLabel(experiment.conclusion_kind);
  return (
    <Section
      index="04"
      title="Controlled experiment"
      hint="Run by your operator. Detail is in the Lab."
    >
      <Panel>
        <div className={styles.panelHead}>
          <StatusMark tone={conclusion.tone} label={conclusion.label} />
        </div>
        <blockquote className={styles.quote}>
          &ldquo;{experiment.conclusion_statement}&rdquo;
        </blockquote>
        <p className={styles.finePrintTight}>
          <Link
            className={styles.rowLink}
            href={`/lab/experiments/${encodeURIComponent(experiment.experiment_id)}`}
          >
            Open the full comparison in the Lab
          </Link>
        </p>
      </Panel>
    </Section>
  );
}
