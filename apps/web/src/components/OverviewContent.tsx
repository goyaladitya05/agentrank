import Link from "next/link";

import { DemandTable } from "@/components/Demand";
import { KeyValueList, StatusMark } from "@/components/Primitives";
import { ScenarioJourneys } from "@/components/ScenarioJourneys";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import shared from "@/components/console.module.css";
import styles from "@/components/overview.module.css";
import { formatMoney, formatTimestamp } from "@/lib/format";
import { conclusionKindLabel } from "@/lib/labels";
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
 * The merchant overview, answering four questions in the order a stranger asks them: what
 * is measured, how the store did, whether something is wrong, and what to do next.
 *
 * The result is one statement set large, the scenario board under it, the issues that need
 * the merchant, and one next step. Everything else the overview insight carries stays on
 * the page behind a disclosure, so no number is lost and none competes with the result.
 *
 * No number here is invented: every count is the API's, the demand figures always say
 * simulated, and an aborted run is labelled before its numbers. The preflight is the
 * server's answer about what this merchant can launch; it may be null when that read failed,
 * and the page still renders everything the overview insight carries.
 */
export function OverviewContent({
  data,
  preflight,
  run: diagnostics = null,
}: {
  data: MerchantOverview;
  preflight: EvaluationPreflight | null;
  /** The latest finished run's missions, for the scenario board. Null when unread. */
  run?: RunDiagnostics | null;
}) {
  const run = latestFinishedRun(data.runs);
  const action = nextAction(data, preflight);
  if (run === null) {
    return <UnmeasuredOverview action={action} />;
  }
  return <MeasuredOverview data={data} run={run} diagnostics={diagnostics} action={action} />;
}

function MeasuredOverview({
  data,
  run,
  diagnostics,
  action,
}: {
  data: MerchantOverview;
  run: RunSummary;
  diagnostics: RunDiagnostics | null;
  action: NextAction;
}) {
  const grouped = groupFindings(data.top_findings);
  const attentionScenarios = distinctMissionCount(grouped.needsAttention);
  return (
    <>
      <header className={styles.hero}>
        <p className={styles.eyebrow}>How well can AI agents shop from your store?</p>
        <h1 className={styles.headline}>
          <strong>{String(run.missions_succeeded)}</strong> of {String(run.purchase_missions)}
          <span className={styles.headlineSub}>purchase scenarios completed</span>
        </h1>
        <p className={styles.reading}>
          {measuredSentence(run)} {heroSentence(run, attentionScenarios)}
        </p>
      </header>

      {diagnostics !== null ? (
        <section className={styles.board} aria-label="What the agents did">
          <ScenarioJourneys journeys={scenarioJourneys(diagnostics.missions)} />
        </section>
      ) : null}

      <AttentionSection findings={grouped.needsAttention} run={run} />

      <NextStep action={action} />

      <MoreAboutThisEvaluation data={data} run={run} />
    </>
  );
}

/** When the measurement happened, and whether it finished. */
function measuredSentence(run: RunSummary): string {
  if (run.status === "ABORTED") {
    return `This evaluation stopped before it finished ${formatTimestamp(run.completed_at)}, so these numbers describe only the scenarios that executed.`;
  }
  return `Measured ${formatTimestamp(run.completed_at)}.`;
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

/** Where a finding leads: to its proposed fix when AgentRank has one, else to the issue. */
function findingAction(finding: MerchantFinding): { href: string; label: string } {
  const reference = finding.compiler_references[0];
  if (reference !== undefined) {
    return { href: `/fixes/${encodeURIComponent(reference.compiler_run_id)}`, label: "Review fix" };
  }
  return { href: `/issues/${encodeURIComponent(finding.key)}`, label: "See the issue" };
}

function AttentionSection({
  findings,
  run,
}: {
  findings: readonly MerchantFinding[];
  run: RunSummary;
}) {
  const safety = safetyReading(run.unsafe_attempts, run.unsafe_completions, 0);
  const providerNote = providerSentence(run.provider_failure_missions);
  return (
    <section className={styles.attention} aria-label="What needs attention">
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionLabel}>What needs attention</h2>
        {findings.length > 0 ? (
          <Link className={styles.sectionLink} href="/issues">
            All issues
          </Link>
        ) : null}
      </div>
      {findings.length === 0 ? (
        <p className={styles.allClear}>
          Nothing needs your attention.
          <span className={styles.allClearBody}>
            {run.status === "ABORTED"
              ? "No merchant-fixable finding came out of the part of this evaluation that executed."
              : "Your latest evaluation produced no finding that requires action from you."}
          </span>
        </p>
      ) : (
        <ol className={styles.attentionList}>
          {findings.slice(0, 3).map((finding) => {
            const next = findingAction(finding);
            return (
              <li key={finding.key} className={styles.attentionItem}>
                <div>
                  <h3 className={styles.attentionTitle}>
                    <Link href={`/issues/${encodeURIComponent(finding.key)}`}>{finding.title}</Link>
                  </h3>
                  {finding.recommendation !== null ? (
                    <p className={styles.attentionBody}>{finding.recommendation}</p>
                  ) : null}
                </div>
                <Link className={styles.attentionAction} href={next.href}>
                  {next.label}
                  <span aria-hidden="true"> &rarr;</span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}
      {providerNote !== null ? <p className={styles.aside}>{providerNote}</p> : null}
      {safety !== null ? <p className={styles.aside}>{safety.text}</p> : null}
    </section>
  );
}

/** The one action the page leads to. */
function NextStep({ action }: { action: NextAction }) {
  return (
    <section className={styles.next} aria-label="Next step">
      <div>
        <p className={styles.eyebrow}>Next step</p>
        <h2 className={styles.nextTitle}>{action.title}</h2>
        <p className={styles.nextBody}>{action.body}</p>
      </div>
      <Link className={shared.primaryButton} href={action.href}>
        {action.label}
        <span aria-hidden="true"> &rarr;</span>
      </Link>
    </section>
  );
}

/**
 * Everything else the overview knows, behind one disclosure: the run's other counts, the
 * simulated demand by currency, the latest controlled experiment and the identifiers.
 * Present on the page and out of the way of the result.
 */
function MoreAboutThisEvaluation({ data, run }: { data: MerchantOverview; run: RunSummary }) {
  const captured = run.simulated_demand
    .map((bucket) => formatMoney(bucket.simulated_captured_demand_amount_minor, bucket.currency))
    .join(" ");
  return (
    <details className={styles.more}>
      <summary>More about this evaluation</summary>
      <div className={styles.moreBody}>
        <KeyValueList
          entries={[
            { term: "Scenarios tested", value: String(run.missions_total) },
            { term: "Successful purchases", value: String(run.missions_succeeded) },
            {
              term: "Correct declines",
              value: `${String(run.correct_abstentions)} of ${String(run.control_missions)}`,
            },
            {
              term: "Provider or system failures",
              value: String(run.provider_failure_missions),
            },
            ...(captured.length > 0
              ? [{ term: "Simulated demand captured", value: captured }]
              : []),
          ]}
        />
        <div className={styles.moreBlock}>
          <p className={shared.finePrintTight}>
            Simulated benchmark figures across your recent evaluations. Not revenue.
          </p>
          <DemandTable
            buckets={data.simulated_demand_totals_by_currency}
            caption="Totals across your recent evaluations, one row per currency."
          />
        </div>
        <ExperimentNote data={data} />
        <p className={shared.finePrintTight}>
          Every evaluation and change is in{" "}
          <Link className={shared.rowLink} href="/history">
            your history
          </Link>
          . Full technical detail, including runs, traces and methodology, is in{" "}
          <Link className={shared.rowLink} href="/lab">
            AgentRank Lab
          </Link>
          .
        </p>
        <TechnicalDetails summary="Technical identifiers">
          <IdRow label="Engine identity" value={data.engine_identity} />
          <IdRow label="Merchant id" value={data.merchant_id} />
        </TechnicalDetails>
      </div>
    </details>
  );
}

/**
 * The latest controlled experiment, when one exists: the backend's conclusion verbatim and
 * a link into the Lab, where the methodology detail lives. NOT_INTERPRETABLE stays exactly
 * that here.
 */
function ExperimentNote({ data }: { data: MerchantOverview }) {
  const experiment = data.latest_experiment;
  if (experiment === null) {
    return null;
  }
  const conclusion = conclusionKindLabel(experiment.conclusion_kind);
  return (
    <div className={styles.moreBlock}>
      <p className={shared.finePrintTight}>
        Controlled experiment, run by your operator:{" "}
        <StatusMark tone={conclusion.tone} label={conclusion.label} />
      </p>
      <blockquote className={shared.quote}>
        &ldquo;{experiment.conclusion_statement}&rdquo;
      </blockquote>
      <p className={shared.finePrintTight}>
        <Link
          className={shared.rowLink}
          href={`/lab/experiments/${encodeURIComponent(experiment.experiment_id)}`}
        >
          Open the full comparison in the Lab
        </Link>
      </p>
    </div>
  );
}

/** The four steps a new merchant walks, with the current one decided by the next action. */
const STEPS = [
  { step: "Import your store", kinds: ["import-store"] },
  { step: "Prepare the evaluation", kinds: ["prepare-evaluation"] },
  { step: "Run the evaluation", kinds: ["run-first-evaluation", "evaluation-in-progress"] },
  { step: "See your results", kinds: [] },
] as const;

function UnmeasuredOverview({ action }: { action: NextAction }) {
  const currentIndex = STEPS.findIndex((entry) =>
    (entry.kinds as readonly string[]).includes(action.kind),
  );
  return (
    <>
      <header className={styles.hero}>
        <p className={styles.eyebrow}>Not measured yet</p>
        <h1 className={styles.question}>
          Can AI shopping agents <em>buy</em> from your store?
        </h1>
        <p className={styles.reading}>
          AgentRank sends shopping agents through realistic purchase scenarios against your store,
          shows you exactly where they fail, and separates what you can fix from what is not your
          problem.
        </p>
      </header>

      <ol className={styles.steps} aria-label="Setup steps">
        {STEPS.map((entry, index) => (
          <li
            key={entry.step}
            className={styles.step}
            data-state={
              index === currentIndex ? "current" : index < currentIndex ? "done" : undefined
            }
          >
            <span className={styles.stepIndex}>0{index + 1}</span>
            {entry.step}
            {index === currentIndex ? <span className={styles.stepNow}>You are here</span> : null}
          </li>
        ))}
      </ol>

      <NextStep action={action} />
    </>
  );
}
