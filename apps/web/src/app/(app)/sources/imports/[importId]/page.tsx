import Link from "next/link";

import { ImportReview } from "@/components/ImportReview";
import { InsightFailure } from "@/components/InsightFailure";
import { Panel, Section } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { decodeSourceImport } from "@/lib/import";
import { confirmImport } from "@/lib/import-actions";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";
export const metadata = { title: "Imported pages | AgentRank" };

export default async function ImportDetailPage({
  params,
}: {
  params: Promise<{ importId: string }>;
}) {
  const { importId } = await params;
  const outcome = await loadInsight(
    `/api/v1/sources/imports/${encodeURIComponent(importId)}`,
    decodeSourceImport,
  );
  if (!outcome.ok) return <InsightFailure failure={outcome.failure} />;
  const found = outcome.data;
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Imported pages</h1>
      </div>
      <Section
        title="What AgentRank read"
        hint="Review this before it becomes your source snapshot."
      >
        <Panel>
          <ImportReview
            found={found}
            action={confirmImport.bind(null, importId, found.stock_level_required)}
          />
        </Panel>
      </Section>
      <Section title="Elsewhere">
        <Panel>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources/import">
              Import a different set of pages
            </Link>
          </p>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources">
              Back to your source history
            </Link>
          </p>
        </Panel>
      </Section>
    </>
  );
}
