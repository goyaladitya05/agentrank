import Link from "next/link";

import { Panel, Section, StatusMark } from "@/components/Primitives";
import { ScenarioJourneys } from "@/components/ScenarioJourneys";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { formatMoney, formatTimestamp } from "@/lib/format";
import {
  actionabilityLabel,
  demandBucketLabel,
  evidenceLevelLabel,
  ownerLabel,
  severityLabel,
  severityTone,
} from "@/lib/labels";
import { scenarioJourneys } from "@/lib/insights/journey";
import { groupFindings } from "@/lib/insights/merchant";
import type { MerchantFinding, RunDiagnostics } from "@/lib/insights/types";

/**
 * The merchant Issues page, set as a ledger: a fixed evidence gutter on the left of every
 * entry, the finding itself in the serif, and two piles a merchant can tell apart from
 * across the room.
 *
 * The split is the diagnostics engine's actionability verbatim. Provider and system
 * failures live under a quieter rule whose heading says no action is required, so they can
 * never read as merchant problems, and every claim links to the evidence that establishes
 * it rather than asserting it bare.
 */
export function IssuesContent({ data }: { data: RunDiagnostics }) {
  const grouped = groupFindings(data.findings);
  return (
    <>
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>Issues</h1>
          <p className={merchant.pageIntro}>
            From your latest evaluation,{" "}
            {data.status === "ABORTED"
              ? `stopped part way ${formatTimestamp(data.completed_at)}. Findings describe only the scenarios that executed.`
              : `completed ${formatTimestamp(data.completed_at)}.`}
          </p>
        </div>
        <TechnicalDetails summary="Technical identifiers">
          <IdRow label="Run id" value={data.run_id} />
          <IdRow label="Engine identity" value={data.engine_identity} />
        </TechnicalDetails>
      </div>

      <section aria-label="Needs your attention">
        <div className={merchant.groupHead} data-tone="warn">
          <h2 className={merchant.groupTitle}>Needs your attention</h2>
          <span className={merchant.groupCount}>{String(grouped.needsAttention.length)}</span>
        </div>
        {grouped.needsAttention.length === 0 ? (
          <div className={styles.emptyState}>
            <p className={styles.emptyTitle}>Nothing needs your attention</p>
            <p>This evaluation produced no finding that requires action from you.</p>
          </div>
        ) : (
          <div className={merchant.entryList}>
            {grouped.needsAttention.map((finding) => (
              <IssueEntry key={finding.key} finding={finding} />
            ))}
          </div>
        )}
      </section>

      <section aria-label="No action required">
        <div className={merchant.groupHead} data-tone="quiet">
          <h2 className={merchant.groupTitle}>No action required from you</h2>
          <span className={merchant.groupCount}>{String(grouped.noActionRequired.length)}</span>
        </div>
        {grouped.noActionRequired.length === 0 ? (
          <div className={styles.emptyState}>
            <p className={styles.emptyTitle}>No provider or system findings</p>
            <p>Nothing external affected this evaluation.</p>
          </div>
        ) : (
          <div className={merchant.entryList}>
            {grouped.noActionRequired.map((finding) => (
              <IssueEntry key={finding.key} finding={finding} />
            ))}
          </div>
        )}
      </section>

      <p className={styles.finePrint}>
        Every scenario, trace and identifier behind these findings is in{" "}
        <Link className={styles.rowLink} href={`/lab/runs/${encodeURIComponent(data.run_id)}`}>
          the Lab view of this run
        </Link>
        .
      </p>
    </>
  );
}

function IssueEntry({ finding }: { finding: MerchantFinding }) {
  const lost = finding.simulated_demand.filter(
    (effect) => effect.bucket.toUpperCase() === "AT_RISK" || effect.bucket.toUpperCase() === "LOST",
  );
  return (
    <article className={merchant.entry}>
      <div className={merchant.entryGutter}>
        <StatusMark
          tone={severityTone(finding.severity)}
          label={severityLabel(finding.severity)}
          description={`Diagnostic code ${finding.code}`}
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
        <div className={merchant.entryMeta}>
          <span>{actionabilityLabel(finding.actionability)}</span>
          {lost.length > 0 ? (
            <span className={merchant.entryConsequence}>
              simulated demand at risk{" "}
              {lost
                .map((effect) => formatMoney(effect.simulated_amount_minor, effect.currency))
                .join(" ")}
            </span>
          ) : null}
        </div>
        {finding.recommendation !== null ? (
          <p className={merchant.entryBody}>{finding.recommendation}</p>
        ) : null}
        <p className={merchant.entryActions}>
          <Link className={styles.rowLink} href={`/issues/${encodeURIComponent(finding.key)}`}>
            Open this issue
          </Link>
          {finding.compiler_references.length > 0 ? (
            <>
              {" · "}
              <Link
                className={styles.rowLink}
                href={`/fixes/${encodeURIComponent(finding.compiler_references[0]?.compiler_run_id ?? "")}`}
              >
                Review the proposed fix
              </Link>
            </>
          ) : null}
        </p>
      </div>
    </article>
  );
}

export function affectedSentence(finding: MerchantFinding): string {
  const scenarios =
    finding.mission_run_ids.length === 1
      ? "1 scenario"
      : `${String(finding.mission_run_ids.length)} scenarios`;
  const products =
    finding.product_ids.length === 0
      ? null
      : finding.product_ids.length === 1
        ? "1 product"
        : `${String(finding.product_ids.length)} products`;
  return products === null ? `${scenarios} affected` : `${scenarios} affected, ${products}`;
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
 * One issue in full: what went wrong, what was affected, why AgentRank believes it, who
 * owns it, and what the merchant can do. The trace-level evidence stays behind links into
 * the Lab; nothing raw is inlined here.
 */
export function IssueDetailContent({
  data,
  finding,
}: {
  data: RunDiagnostics;
  finding: MerchantFinding;
}) {
  const yours =
    finding.actionability === "MERCHANT_ACTION" || finding.actionability === "REVIEW_REQUIRED";
  const lost = finding.simulated_demand.filter(
    (effect) => effect.bucket.toUpperCase() === "AT_RISK" || effect.bucket.toUpperCase() === "LOST",
  );
  // The run's own missions for the scenarios this finding names, so the journeys drawn below
  // are the recorded attempts rather than a restatement of the finding.
  const affected = data.missions.filter((mission) =>
    finding.mission_run_ids.includes(mission.mission_run_id),
  );
  return (
    <>
      <header className={merchant.masthead}>
        <p className={merchant.eyebrow}>
          <Link className={merchant.secondaryAction} href="/issues">
            Issues
          </Link>{" "}
          / {severityLabel(finding.severity)}
        </p>
        <h1 className={merchant.mastStatementSub}>{finding.title}</h1>
        <p className={merchant.mastReading}>
          {yours
            ? "This is something you can change."
            : "No merchant action is required. This is not a problem with your store."}
        </p>
      </header>

      <Section index="01" title="Who owns this">
        <Panel>
          <p>
            <StatusMark
              tone={yours ? "warn" : "neutral"}
              label={actionabilityLabel(finding.actionability)}
            />
          </p>
          <p className={styles.finePrintTight}>Attributed to: {ownerLabel(finding.owner)}.</p>
        </Panel>
      </Section>

      <Section index="02" title="What was affected" hint="Where each attempt stopped.">
        {affected.length > 0 ? (
          <ScenarioJourneys
            journeys={scenarioJourneys(affected)}
            caption="Each row is one shopping attempt this issue touched, drawn to the stage AgentRank's records say it reached."
          />
        ) : null}
        <Panel>
          <p>{affectedSentence(finding)}.</p>
          {finding.mission_keys.length > 0 ? (
            <ul className={styles.launchTerms}>
              {finding.mission_run_ids.map((missionRunId, index) => (
                <li key={missionRunId}>
                  <span className={styles.mono}>{finding.mission_keys[index] ?? "scenario"}</span>{" "}
                  <Link
                    className={styles.rowLink}
                    href={`/lab/runs/${encodeURIComponent(data.run_id)}/missions/${encodeURIComponent(missionRunId)}`}
                  >
                    View evidence
                  </Link>
                </li>
              ))}
            </ul>
          ) : null}
          {finding.attribute_keys.length > 0 ? (
            <p className={styles.finePrintTight}>
              Product information involved:{" "}
              <span className={styles.mono}>{finding.attribute_keys.join(", ")}</span>
            </p>
          ) : null}
          {lost.length > 0 ? (
            <p className={styles.finePrintTight}>
              Simulated demand attributed to this issue:{" "}
              {lost
                .map(
                  (effect) =>
                    `${demandBucketLabel(effect.bucket)} ${formatMoney(
                      effect.simulated_amount_minor,
                      effect.currency,
                    )}`,
                )
                .join(", ")}
              . Simulated benchmark figures, not revenue.
            </p>
          ) : null}
        </Panel>
      </Section>

      <Section index="03" title="Why AgentRank believes this">
        <Panel>
          {finding.attribute_keys.length > 0 ? (
            <p className={merchant.establishRow}>
              <span className={merchant.establishLabel}>Could not establish</span>
              <span className={merchant.establishValue}>{finding.attribute_keys.join(", ")}</span>
            </p>
          ) : null}
          <p>{evidenceSentence(finding.evidence_level)}</p>
          <p className={styles.finePrintTight}>
            The recorded scenarios above carry the full evidence trail, down to individual trace
            events, in the Lab.
          </p>
        </Panel>
      </Section>

      <Section index="04" title="What you can do">
        <Panel>
          {finding.recommendation !== null ? (
            <p>{finding.recommendation}</p>
          ) : (
            <p>
              {yours
                ? "No specific recommendation was produced for this finding."
                : "Nothing. This finding requires no action from you."}
            </p>
          )}
          {finding.compiler_references.length > 0 ? (
            <p className={styles.finePrintTight}>
              AgentRank proposed {finding.compiler_references.length === 1 ? "a fix" : "fixes"} for
              this:{" "}
              <Link
                className={styles.rowLink}
                href={`/fixes/${encodeURIComponent(
                  finding.compiler_references[0]?.compiler_run_id ?? "",
                )}`}
              >
                review {finding.compiler_references.length === 1 ? "it" : "them"}
              </Link>
              .
            </p>
          ) : null}
        </Panel>
      </Section>

      <TechnicalDetails summary="Technical details">
        <IdRow label="Diagnostic code" value={finding.code} />
        <IdRow label="Finding key" value={finding.key} />
        <IdRow label="Evidence level" value={finding.evidence_level} />
        <IdRow label="Run id" value={data.run_id} />
      </TechnicalDetails>
    </>
  );
}
