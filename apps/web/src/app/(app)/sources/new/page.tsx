import { randomUUID } from "node:crypto";

import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { Panel, Section } from "@/components/Primitives";
import { SubmitSource } from "@/components/SubmitSource";
import { TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import { loadInsight } from "@/lib/insights/load";
import { decodeSourceOverview, decodeSourceSnapshot, documentText } from "@/lib/source";
import { submitSource } from "@/lib/source-actions";

export const dynamic = "force-dynamic";
export const metadata = { title: "Supply source evidence | AgentRank" };

/** The shape a source document has, shown rather than prefilled for a merchant who has none. */
const SHAPE = `{
  "products": [
    {
      "external_id": "YOUR-SKU-GROUP",
      "title": "Product title",
      "description": "What you say about it.",
      "category": "category",
      "variants": [
        {
          "sku": "YOUR-SKU",
          "label": "Black",
          "price_amount_minor": 499900,
          "currency": "INR",
          "inventory_quantity": 24,
          "merchant_metadata": { "finish": "black" }
        }
      ],
      "merchant_metadata": {}
    }
  ],
  "policy_text": {
    "warranty": "What your warranty says."
  }
}`;

export default async function NewSourcePage() {
  const overview = await loadInsight("/api/v1/sources?limit=1", decodeSourceOverview);
  if (!overview.ok) return <InsightFailure failure={overview.failure} />;
  const currentId = overview.data.current_source_snapshot_id;

  let initialDocument = "";
  if (currentId !== null) {
    const current = await loadInsight(
      `/api/v1/sources/${encodeURIComponent(currentId)}`,
      decodeSourceSnapshot,
    );
    if (!current.ok) return <InsightFailure failure={current.failure} />;
    initialDocument = documentText(current.data.document);
  }

  // One rendered form is one submission. The key is generated here, so submitting this form
  // twice or retrying after a lost response is the same command; opening the page again is a
  // new key and therefore a deliberate second one.
  const requestKey = randomUUID();
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>Supply source evidence</h1>
      </div>
      <Section
        title="Source document"
        hint="Your own words about your catalog, in AgentRank's canonical format."
      >
        <Panel>
          <SubmitSource
            initialDocument={initialDocument}
            hasCurrentSource={currentId !== null}
            action={submitSource.bind(null, requestKey)}
          />
        </Panel>
      </Section>
      <Section title="What this document is">
        <Panel>
          <p>
            A source document is what you say about your catalog. The compiler reads it and proposes
            typed facts, citing the exact field behind each one, and you decide which of them become
            published truth.
          </p>
          <p className={styles.reviewMeta}>
            It is not your commerce runtime. Price, currency, stock, checkout, mandates and payments
            are held elsewhere and nothing you write here changes any of them.
          </p>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources/import">
              Or import it from your own public pages
            </Link>
          </p>
          <p className={styles.reviewMeta}>
            <Link className={styles.rowLink} href="/sources">
              Back to your source history
            </Link>
          </p>
          <TechnicalDetails summary="Document shape">
            <pre className={styles.tracePayload}>{SHAPE}</pre>
            <p className={styles.reviewMeta}>
              Identifiers may use letters, digits, hyphens and underscores. A product needs at least
              one variant. `merchant_metadata` maps names to strings, whole numbers or true and
              false.
            </p>
          </TechnicalDetails>
        </Panel>
      </Section>
    </>
  );
}
