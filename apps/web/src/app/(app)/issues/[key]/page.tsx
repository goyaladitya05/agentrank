import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { IssueDetailContent } from "@/components/Issues";
import { EmptyState, Panel } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { loadLatestRunDiagnostics } from "@/lib/insights/latest-run";

export const dynamic = "force-dynamic";
export const metadata = { title: "Issue | AgentRank" };

/**
 * One issue from the latest finished evaluation, addressed by its finding key.
 *
 * The key is stable within a run but a newer evaluation produces new findings, so an old
 * link may stop resolving. That is answered as a fact with the way back, not as an error:
 * the issue list is always the current truth.
 */
export default async function IssuePage({ params }: { params: Promise<{ key: string }> }) {
  const { key } = await params;
  const decodedKey = decodeURIComponent(key);
  const outcome = await loadLatestRunDiagnostics();
  if (outcome.state === "failure") {
    return <InsightFailure failure={outcome.failure} />;
  }
  const finding =
    outcome.state === "ok"
      ? (outcome.data.findings.find((entry) => entry.key === decodedKey) ?? null)
      : null;
  if (outcome.state === "none" || finding === null) {
    return (
      <>
        <div className={styles.pageHeader}>
          <h1 className={styles.pageTitle}>Issue</h1>
        </div>
        <Panel>
          <EmptyState
            title="This issue is not on your latest evaluation"
            explanation="Findings belong to the evaluation that produced them. A newer evaluation replaces the issue list, so an older link can stop resolving here."
          >
            <Link className={styles.textLink} href="/issues">
              See your current issues
            </Link>
          </EmptyState>
        </Panel>
      </>
    );
  }
  return <IssueDetailContent data={outcome.data} finding={finding} />;
}
