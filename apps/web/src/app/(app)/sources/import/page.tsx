import { randomUUID } from "node:crypto";

import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Panel, Section, StatusMark } from "@/components/Primitives";
import { RunImport } from "@/components/RunImport";
import { TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import { decodeImportHistory, type ImportSummary } from "@/lib/import";
import { runImport } from "@/lib/import-actions";
import { loadInsight } from "@/lib/insights/load";

export const dynamic = "force-dynamic";
export const metadata = { title: "Import your pages | AgentRank" };

export default async function ImportSourcePage() {
  const history = await loadInsight("/api/v1/sources/imports?limit=10", decodeImportHistory);
  if (!history.ok) return <InsightFailure failure={history.failure} />;

  // One rendered form is one import. The key is generated here, so submitting this form twice or
  // retrying after a lost response fetches the merchant's storefront once; opening the page again
  // is a new key and therefore a deliberate second import.
  const requestKey = randomUUID();
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Import your pages</h1>
      </div>
      <Section
        title="Public pages to read"
        hint="AgentRank reads them once and shows you what it found."
      >
        <Panel>
          <RunImport action={runImport.bind(null, requestKey)} />
        </Panel>
      </Section>
      <Section title="What this does and does not do">
        <Panel>
          <p>
            An import turns what your pages already publish into a source draft. You review the
            draft, and only then does it become a source snapshot the compiler can read.
          </p>
          <p className={styles.reviewMeta}>
            It is not your commerce runtime. Price, currency, stock, checkout, mandates and payments
            are held elsewhere and nothing an import does changes any of them.
          </p>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources/new">
              Or write your source document yourself
            </Link>
          </p>
          <TechnicalDetails summary="What AgentRank reads">
            <p className={styles.reviewMeta}>
              Schema.org product data first, then the Open Graph and product metadata tags a page
              publishes. A page that publishes neither, states a price with no currency, states two
              prices that disagree, or publishes more than one product is reported rather than
              guessed at.
            </p>
            <p className={styles.reviewMeta}>
              AgentRank requests each page once over ordinary HTTP, follows at most three redirects,
              reads at most two megabytes per page, and stops at fifteen seconds. It does not follow
              links found on your pages, so it reads what you name and nothing else.
            </p>
          </TechnicalDetails>
        </Panel>
      </Section>
      <Section title="Recent imports" hint="Newest first. An import is a record of what was read.">
        <History imports={history.data} />
      </Section>
    </>
  );
}

function History({ imports }: { imports: readonly ImportSummary[] }) {
  if (imports.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No imports yet"
          explanation="When you import your pages, every attempt appears here with what it read and what it could not."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Recent imports">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Storefront</th>
            <th scope="col">Read</th>
            <th scope="col">Pages</th>
            <th scope="col">Extracted</th>
            <th scope="col">Source snapshot</th>
          </tr>
        </thead>
        <tbody>
          {imports.map((entry) => (
            <tr key={entry.import_id}>
              <td>
                <Link
                  className={styles.rowLinkStrong}
                  href={`/sources/imports/${encodeURIComponent(entry.import_id)}`}
                >
                  {entry.origin}
                </Link>
                {entry.state === "FAILED" ? (
                  <>
                    <br />
                    <StatusMark tone="fail" label="Did not finish" />
                  </>
                ) : null}
              </td>
              <td>{formatTimestamp(entry.created_at)}</td>
              <td>
                {String(entry.retrieved_count)} of {String(entry.page_count)}
              </td>
              <td>
                {String(entry.product_count)} product(s)
                <br />
                <span className={styles.cellMuted}>
                  {entry.omission_count > 0
                    ? `${String(entry.omission_count)} not imported`
                    : entry.state === "COMPLETED"
                      ? "Nothing left out"
                      : "Did not finish"}
                </span>
              </td>
              <td>
                {entry.source_snapshot_id === null ? (
                  <span className={styles.cellMuted}>Not created</span>
                ) : (
                  <Link
                    className={styles.rowLink}
                    href={`/sources/${encodeURIComponent(entry.source_snapshot_id)}`}
                  >
                    Created
                  </Link>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
