import Link from "next/link";

import { formatMoney } from "@/lib/format";
import {
  actionabilityLabel,
  actionabilityTone,
  demandBucketLabel,
  ownerLabel,
  severityLabel,
  severityTone,
} from "@/lib/labels";
import type { MerchantFinding } from "@/lib/insights/types";

import { EmptyState, StatusMark } from "./Primitives";
import styles from "./console.module.css";

/**
 * Grouped findings, one of the strongest product surfaces in the console.
 *
 * Every finding shows what happened in the backend's own merchant sentence, who owns it,
 * whether the merchant can act on it, how many missions it affected, and the simulated
 * demand attributed to it. Raw diagnostic codes stay secondary.
 */
export function FindingList({
  findings,
  runId,
}: {
  findings: readonly MerchantFinding[];
  /** The run the missions belong to, so mission links can be built. */
  runId: string | null;
}) {
  if (findings.length === 0) {
    return (
      <div className={styles.panel}>
        <EmptyState
          title="No findings"
          explanation="Nothing in this evidence rose to a grouped diagnosis. That is an absence of findings, not a guarantee."
        />
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      {findings.map((finding) => (
        <FindingArticle key={finding.key} finding={finding} runId={runId} />
      ))}
    </div>
  );
}

function FindingArticle({ finding, runId }: { finding: MerchantFinding; runId: string | null }) {
  return (
    <article className={styles.finding}>
      <div className={styles.findingTop}>
        <StatusMark
          tone={severityTone(finding.severity)}
          label={severityLabel(finding.severity)}
          description={`Diagnostic code ${finding.code}`}
        />
        <h3 className={styles.findingTitle}>{finding.title}</h3>
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
        <MissionLinks finding={finding} runId={runId} />
        {finding.attribute_keys.length > 0 ? (
          <span>
            Attributes: <span className={styles.mono}>{finding.attribute_keys.join(", ")}</span>
          </span>
        ) : null}
        {finding.product_ids.length > 0 ? (
          <span title={finding.product_ids.join(", ")}>
            {finding.product_ids.length} product{finding.product_ids.length === 1 ? "" : "s"} ·{" "}
            {finding.variant_ids.length} variant{finding.variant_ids.length === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>
      {finding.simulated_demand.length > 0 ? (
        <p className={styles.findingDemand}>
          {finding.simulated_demand
            .map(
              (effect) =>
                `${demandBucketLabel(effect.bucket)}: ${formatMoney(
                  effect.simulated_amount_minor,
                  effect.currency,
                )}`,
            )
            .join(" · ")}
        </p>
      ) : null}
      {finding.recommendation !== null ? (
        <p className={styles.findingRecommendation}>{finding.recommendation}</p>
      ) : null}
    </article>
  );
}

function MissionLinks({ finding, runId }: { finding: MerchantFinding; runId: string | null }) {
  if (runId === null || finding.mission_run_ids.length === 0) {
    const count = finding.mission_run_ids.length;
    return (
      <span>
        {count} affected mission{count === 1 ? "" : "s"}
      </span>
    );
  }
  return (
    <span>
      {finding.mission_run_ids.map((missionRunId, index) => (
        <span key={missionRunId}>
          {index > 0 ? ", " : ""}
          <Link
            className={styles.rowLink}
            href={`/runs/${encodeURIComponent(runId)}/missions/${encodeURIComponent(missionRunId)}`}
          >
            {finding.mission_keys[index] ?? "mission"}
          </Link>
        </span>
      ))}
    </span>
  );
}
