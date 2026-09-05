import Link from "next/link";

import { StatusMark } from "@/components/Primitives";
import { ScenarioJourneys } from "@/components/ScenarioJourneys";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import shared from "@/components/console.module.css";
import styles from "@/components/issues.module.css";
import { formatMoney, formatTimestamp } from "@/lib/format";
import {
  actionabilityLabel,
  evidenceLevelLabel,
  ownerLabel,
  severityLabel,
  severityTone,
} from "@/lib/labels";
import { scenarioJourneys, scenarioName } from "@/lib/insights/journey";
import { groupFindings } from "@/lib/insights/merchant";
import type { MerchantFinding, RunDiagnostics } from "@/lib/insights/types";

/**
 * The merchant Issues page: a prioritised list, not an issue tracker.
 *
 * Every entry answers three questions in the merchant's words: what went wrong, why it
 * matters and what they can do. The split into two groups is the diagnostics engine's
 * actionability verbatim, so a provider failure can never read as merchant work, and
 * every claim links to the evidence that establishes it rather than asserting it bare.
 */
export function IssuesContent({ data }: { data: RunDiagnostics }) {
  const grouped = groupFindings(data.findings);
  return (
    <>
      <div className={shared.pageHeader}>
        <div>
          <h1 className={shared.pageTitle}>Issues</h1>
          <p className={shared.pageIntro}>
            From your latest evaluation,{" "}
            {data.status === "ABORTED"
              ? `stopped part way ${formatTimestamp(data.completed_at)}. Findings describe only the scenarios that executed.`
              : `completed ${formatTimestamp(data.completed_at)}.`}
          </p>
        </div>
      </div>

      <section aria-label="Needs your attention" className={styles.group} data-tone="warn">
        <div className={styles.groupHead}>
          <h2 className={styles.groupTitle}>Needs your attention</h2>
          <span className={styles.groupCount}>{String(grouped.needsAttention.length)}</span>
        </div>
        {grouped.needsAttention.length === 0 ? (
          <p className={styles.empty}>
            Nothing needs your attention.
            <span className={styles.emptyBody}>
              This evaluation produced no finding that requires action from you.
            </span>
          </p>
        ) : (
          <ol className={styles.list}>
            {grouped.needsAttention.map((finding) => (
              <IssueEntry key={finding.key} finding={finding} />
            ))}
          </ol>
        )}
      </section>

      <section aria-label="No action required" className={styles.group}>
        <div className={styles.groupHead}>
          <h2 className={styles.groupTitle}>No action required from you</h2>
          <span className={styles.groupCount}>{String(grouped.noActionRequired.length)}</span>
        </div>
        {grouped.noActionRequired.length === 0 ? (
          <p className={styles.empty}>
            No provider or system findings.
            <span className={styles.emptyBody}>Nothing external affected this evaluation.</span>
          </p>
        ) : (
          <ol className={styles.list}>
            {grouped.noActionRequired.map((finding) => (
              <IssueEntry key={finding.key} finding={finding} />
            ))}
          </ol>
        )}
      </section>

      <p className={shared.finePrint}>
        Every scenario, trace and identifier behind these findings is in{" "}
        <Link className={shared.rowLink} href={`/lab/runs/${encodeURIComponent(data.run_id)}`}>
          the Lab view of this run
        </Link>
        .
      </p>
      <TechnicalDetails summary="Technical identifiers">
        <IdRow label="Run id" value={data.run_id} />
        <IdRow label="Engine identity" value={data.engine_identity} />
      </TechnicalDetails>
    </>
  );
}

/** Whether the diagnostics engine put this finding in the merchant's hands. */
function isYours(finding: MerchantFinding): boolean {
  return finding.actionability === "MERCHANT_ACTION" || finding.actionability === "REVIEW_REQUIRED";
}

/** The simulated demand the engine attributed to this finding as lost or at risk. */
function demandAtRisk(finding: MerchantFinding): string | null {
  const lost = finding.simulated_demand.filter(
    (effect) => effect.bucket.toUpperCase() === "AT_RISK" || effect.bucket.toUpperCase() === "LOST",
  );
  if (lost.length === 0) {
    return null;
  }
  return lost
    .map((effect) => formatMoney(effect.simulated_amount_minor, effect.currency))
    .join(" and ");
}

/**
 * Why a finding matters, in one sentence: how many shopping scenarios it stopped and what
 * simulated demand went with them. Provider findings say plainly that they are not the
 * store's problem.
 */
export function whyItMatters(finding: MerchantFinding): string {
  const count = finding.mission_run_ids.length;
  const scenarios =
    count === 1
      ? "1 shopping scenario stopped on this"
      : `${String(count)} shopping scenarios stopped on this`;
  const lost = demandAtRisk(finding);
  const sentence =
    lost === null ? `${scenarios}.` : `${scenarios}, with ${lost} of simulated demand at risk.`;
  if (isYours(finding)) {
    return sentence;
  }
  return `${sentence} It is not a problem with your store and needs nothing from you.`;
}

/** What the merchant can do, or the honest absence of anything to do. */
function whatYouCanDo(finding: MerchantFinding): string {
  if (finding.recommendation !== null) {
    return finding.recommendation;
  }
  return isYours(finding)
    ? "No specific recommendation was produced for this finding."
    : "Nothing. This finding requires no action from you.";
}

/** The proposed fix a finding leads to, when AgentRank has one at this address. */
function fixHref(finding: MerchantFinding): string | null {
  const reference = finding.compiler_references[0];
  return reference === undefined
    ? null
    : `/fixes/${encodeURIComponent(reference.compiler_run_id)}#${encodeURIComponent(reference.candidate_id)}`;
}

function IssueEntry({ finding }: { finding: MerchantFinding }) {
  const detail = `/issues/${encodeURIComponent(finding.key)}`;
  const fix = fixHref(finding);
  return (
    <li className={styles.issue}>
      <div>
        <p className={styles.issueMeta}>
          <StatusMark
            tone={severityTone(finding.severity)}
            label={severityLabel(finding.severity)}
            description={`Diagnostic code ${finding.code}`}
          />
          <span>{actionabilityLabel(finding.actionability)}</span>
        </p>
        <h3 className={styles.issueTitle}>
          <Link href={detail}>{finding.title}</Link>
        </h3>
        <dl className={styles.facts}>
          <div>
            <dt>Why it matters</dt>
            <dd>{whyItMatters(finding)}</dd>
          </div>
          <div>
            <dt>What you can do</dt>
            <dd>{whatYouCanDo(finding)}</dd>
          </div>
        </dl>
      </div>
      <div className={styles.issueActions}>
        {fix !== null ? (
          <Link className={shared.primaryButton} href={fix}>
            Review fix
            <span aria-hidden="true"> &rarr;</span>
          </Link>
        ) : null}
        <Link className={styles.issueLink} href={detail}>
          Open this issue
          <span aria-hidden="true"> &rarr;</span>
        </Link>
      </div>
    </li>
  );
}

/** Merchant words for what an evidence level means about how sure AgentRank is. */
export function evidenceSentence(level: string): string {
  switch (level) {
    case "TRUSTED_FACT":
      return "Established from trusted commerce records of what actually happened, not from the agent's own account.";
    case "DETERMINISTIC_ATTRIBUTION":
      return "Attributed deterministically from the recorded behavior of the shopping agent against your published information.";
    case "UNRESOLVED":
      return "AgentRank could not fully resolve the cause. Treat this as a lead rather than a verdict.";
    default:
      return evidenceLevelLabel(level);
  }
}

/**
 * One issue in full: what went wrong, where each attempt stopped, why it matters, what the
 * merchant can do, and why AgentRank believes it. The trace-level evidence stays behind
 * links into the Lab; nothing raw is inlined here.
 */
export function IssueDetailContent({
  data,
  finding,
}: {
  data: RunDiagnostics;
  finding: MerchantFinding;
}) {
  const yours = isYours(finding);
  const fix = fixHref(finding);
  // The run's own missions for the scenarios this finding names, so the journeys drawn below
  // are the recorded attempts rather than a restatement of the finding.
  const affected = data.missions.filter((mission) =>
    finding.mission_run_ids.includes(mission.mission_run_id),
  );
  return (
    <>
      <Link className={shared.backLink} href="/issues">
        <span aria-hidden="true">&larr; </span>Issues
      </Link>
      <header className={styles.detailHead}>
        <p className={styles.detailMeta}>
          <StatusMark
            tone={severityTone(finding.severity)}
            label={severityLabel(finding.severity)}
            description={`Diagnostic code ${finding.code}`}
          />
          <span>{actionabilityLabel(finding.actionability)}</span>
        </p>
        <h1 className={styles.detailTitle}>{finding.title}</h1>
        <p className={styles.detailLead}>
          {yours
            ? "This is something you can change."
            : "No merchant action is required. This is not a problem with your store."}
        </p>
      </header>

      {affected.length > 0 ? (
        <section className={styles.board} aria-label="Where each attempt stopped">
          <p className={styles.boardLabel}>Where each attempt stopped</p>
          <ScenarioJourneys
            journeys={scenarioJourneys(affected)}
            caption="Each row is one shopping attempt this issue touched, drawn to the stage AgentRank's records say it reached."
          />
        </section>
      ) : null}

      <div className={styles.columns}>
        <section className={styles.column} aria-label="Why it matters">
          <h2>Why it matters</h2>
          {finding.attribute_keys.length > 0 ? (
            <span className={styles.missing}>
              Could not establish: {finding.attribute_keys.join(", ")}
            </span>
          ) : null}
          <p>{whyItMatters(finding)}</p>
          {demandAtRisk(finding) !== null ? (
            <p className={shared.finePrintTight}>Simulated benchmark figures, not revenue.</p>
          ) : null}
        </section>

        <section className={styles.column} aria-label="What you can do">
          <h2>What you can do</h2>
          <p>{whatYouCanDo(finding)}</p>
          {fix !== null ? (
            <p className={styles.columnAction}>
              <Link className={shared.primaryButton} href={fix}>
                Review fix
                <span aria-hidden="true"> &rarr;</span>
              </Link>
            </p>
          ) : null}
        </section>

        <section className={styles.column} aria-label="Why AgentRank believes this">
          <h2>Why AgentRank believes this</h2>
          <p>{evidenceSentence(finding.evidence_level)}</p>
          <p>Who owns this: {ownerLabel(finding.owner)}.</p>
          {finding.mission_keys.length > 0 ? (
            <ul className={styles.evidenceList}>
              {finding.mission_run_ids.map((missionRunId, index) => (
                <li key={missionRunId}>
                  <span>{scenarioName(finding.mission_keys[index] ?? "scenario")}</span>
                  <Link
                    className={shared.rowLink}
                    href={`/lab/runs/${encodeURIComponent(data.run_id)}/missions/${encodeURIComponent(missionRunId)}`}
                  >
                    View evidence
                  </Link>
                </li>
              ))}
            </ul>
          ) : null}
        </section>
      </div>

      <TechnicalDetails summary="Technical details">
        <IdRow label="Diagnostic code" value={finding.code} />
        <IdRow label="Finding key" value={finding.key} />
        <IdRow label="Evidence level" value={finding.evidence_level} />
        <IdRow label="Run id" value={data.run_id} />
        {finding.product_ids.map((productId) => (
          <IdRow key={productId} label="Product id" value={productId} />
        ))}
      </TechnicalDetails>
    </>
  );
}
