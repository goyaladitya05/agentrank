import Link from "next/link";

import { DemandTable } from "@/components/Demand";
import { OutcomeBar } from "@/components/OutcomeBar";
import { Panel, Section, StatusMark } from "@/components/Primitives";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { formatMoney, formatTimestamp } from "@/lib/format";
import { conclusionKindLabel, severityLabel, severityTone } from "@/lib/labels";
import { providerSentence, safetyReading } from "@/lib/insights/summary";
import { groupFindings, latestFinishedRun, nextAction } from "@/lib/insights/merchant";
import type { NextAction } from "@/lib/insights/merchant";
import type { EvaluationPreflight } from "@/lib/evaluation";
import type { MerchantFinding, MerchantOverview, RunSummary } from "@/lib/insights/types";

/**
 * The merchant overview: what happened when AI shopping agents tried to buy from this
 * store, what needs attention, and the one action to take next.
 *
 * The preflight is the server's answer about what this merchant can launch and whether one
 * is pending; it may be null when that read failed, and the page still renders everything
 * the overview insight carries. No number here is invented: every count is the API's, the
 * demand figures always say simulated, and an aborted run is labelled before its numbers.
 */
export function OverviewContent({
  data,
  preflight,
}: {
  data: MerchantOverview;
  preflight: EvaluationPreflight | null;
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
        <NewMerchantHero action={action} />
      ) : (
        <MeasuredHero run={run} findings={data.top_findings} action={action} />
      )}

      {run !== null ? <AttentionSection data={data} run={run} /> : null}

      {run !== null ? (
        <Section
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

function NewMerchantHero({ action }: { action: NextAction }) {
  const currentIndex = JOURNEY.findIndex((entry) =>
    (entry.kinds as readonly string[]).includes(action.kind),
  );
  return (
    <div className={merchant.hero}>
      <div className={merchant.heroPerformance}>
        <p className={merchant.eyebrow}>AI shopping performance</p>
        <h2 className={merchant.heroHeadline}>Can AI shopping agents buy from your store?</h2>
        <p className={merchant.heroSub}>
          AgentRank sends shopping agents through realistic purchase scenarios against your store,
          shows you exactly where they fail, and separates what you can fix from what is not your
          problem.
        </p>
        <ol className={styles.launchTerms} aria-label="Setup steps">
          {JOURNEY.map((entry, index) => (
            <li key={entry.step}>
              {index === currentIndex ? <strong>{entry.step}</strong> : entry.step}
              {index === currentIndex ? " (you are here)" : ""}
            </li>
          ))}
        </ol>
      </div>
      <ActionCard action={action} />
    </div>
  );
}

function MeasuredHero({
  run,
  findings,
  action,
}: {
  run: RunSummary;
  findings: readonly MerchantFinding[];
  action: NextAction;
}) {
  const grouped = groupFindings(findings);
  const merchantScenarios = distinctMissionCount(grouped.needsAttention);
  const unfinished =
    run.missions_total -
    (run.missions_succeeded + run.missions_failed + run.missions_abstained + run.missions_errored);
  return (
    <div className={merchant.hero}>
      <div className={merchant.heroPerformance}>
        <p className={merchant.eyebrow}>AI shopping performance</p>
        <h2 className={merchant.heroHeadline}>
          <strong>{String(run.missions_succeeded)}</strong> of{" "}
          <strong>{String(run.purchase_missions)}</strong> purchase scenarios completed
        </h2>
        <p className={merchant.heroSub}>
          {heroSentence(run, merchantScenarios)}{" "}
          {run.status === "ABORTED"
            ? "This evaluation stopped before it finished, so these numbers describe only the scenarios that executed."
            : ""}
        </p>
        <div className={merchant.heroTrack}>
          <OutcomeBar
            counts={{
              succeeded: run.missions_succeeded,
              failed: run.missions_failed,
              abstained: run.missions_abstained,
              errored: run.missions_errored,
              unfinished: Math.max(unfinished, 0),
            }}
          />
        </div>
        <div className={merchant.statStrip}>
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
        <p className={styles.finePrint}>
          Latest evaluation {run.status === "ABORTED" ? "stopped" : "completed"}{" "}
          {formatTimestamp(run.completed_at)}.
        </p>
      </div>
      <ActionCard action={action} />
    </div>
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

function ActionCard({ action }: { action: NextAction }) {
  return (
    <aside className={merchant.actionCard} aria-label="Next step">
      <p className={merchant.actionEyebrow}>Next step</p>
      <h2 className={merchant.actionTitle}>{action.label}</h2>
      <p className={merchant.actionBody}>{action.body}</p>
      <p className={merchant.actionFoot}>
        <Link className={merchant.primaryButton} href={action.href}>
          {action.label}
        </Link>
      </p>
    </aside>
  );
}

function AttentionSection({ data, run }: { data: MerchantOverview; run: RunSummary }) {
  const grouped = groupFindings(data.top_findings);
  const safety = safetyReading(run.unsafe_attempts, run.unsafe_completions, 0);
  const providerNote = providerSentence(run.provider_failure_missions);
  return (
    <Section
      title="What needs attention"
      actions={
        grouped.needsAttention.length > 0 ? (
          <Link className={styles.textLink} href="/issues">
            All issues
          </Link>
        ) : undefined
      }
    >
      <div className={styles.panel}>
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
          grouped.needsAttention.slice(0, 5).map((finding) => (
            <article key={finding.key} className={merchant.issueCard}>
              <div className={merchant.issueTitleRow}>
                <StatusMark
                  tone={severityTone(finding.severity)}
                  label={severityLabel(finding.severity)}
                />
                <h3 className={merchant.issueTitle}>
                  <Link
                    className={merchant.issueTitleLink}
                    href={`/issues/${encodeURIComponent(finding.key)}`}
                  >
                    {finding.title}
                  </Link>
                </h3>
              </div>
              <div className={merchant.issueFacts}>
                <span>
                  {finding.mission_run_ids.length === 1
                    ? "1 scenario affected"
                    : `${String(finding.mission_run_ids.length)} scenarios affected`}
                </span>
                {finding.product_ids.length > 0 ? (
                  <span>
                    {finding.product_ids.length === 1
                      ? "1 product"
                      : `${String(finding.product_ids.length)} products`}
                  </span>
                ) : null}
              </div>
            </article>
          ))
        )}
      </div>
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
    <Section title="Controlled experiment" hint="Run by your operator. Detail is in the Lab.">
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
