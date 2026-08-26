import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { IssuesContent } from "@/components/Issues";
import { EmptyState, Panel } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { loadLatestRunDiagnostics } from "@/lib/insights/latest-run";

export const dynamic = "force-dynamic";
export const metadata = { title: "Issues | AgentRank" };

export default async function IssuesPage() {
  const outcome = await loadLatestRunDiagnostics();
  if (outcome.state === "failure") {
    return <InsightFailure failure={outcome.failure} />;
  }
  if (outcome.state === "none") {
    return (
      <>
        <div className={styles.pageHeader}>
          <h1 className={styles.pageTitle}>Issues</h1>
        </div>
        <Panel>
          <EmptyState
            title="No evaluation has finished yet"
            explanation="Issues come out of an evaluation: AI shopping agents attempt realistic purchases against your store, and what stopped them appears here."
          >
            <Link className={styles.textLink} href="/overview">
              Start from your overview
            </Link>
          </EmptyState>
        </Panel>
      </>
    );
  }
  return <IssuesContent data={outcome.data} />;
}
