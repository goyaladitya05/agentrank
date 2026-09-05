import Link from "next/link";

import { InsightFailure } from "@/components/InsightFailure";
import { EmptyState, Panel, StatusMark } from "@/components/Primitives";
import shared from "@/components/console.module.css";
import styles from "@/components/history.module.css";
import { decodeCompilerOverview } from "@/lib/compiler";
import { decodeEvaluationLaunchList } from "@/lib/evaluation";
import { formatTimestamp } from "@/lib/format";
import { loadInsight } from "@/lib/insights/load";
import { composeHistory, type MerchantEvent } from "@/lib/insights/merchant";
import { decodeSourceOverview } from "@/lib/source";

export const dynamic = "force-dynamic";
export const metadata = { title: "History | AgentRank" };

/**
 * The merchant's history: evaluations, store updates and fix batches as one stream.
 *
 * The unit is the thing the merchant did or asked for, not the benchmark run underneath
 * it. Each event links to its own detail page, and the raw run inventory stays in the Lab.
 */
export default async function HistoryPage() {
  const launches = await loadInsight("/api/v1/benchmark/evaluations?limit=20", (value) =>
    decodeEvaluationLaunchList(value),
  );
  if (!launches.ok) return <InsightFailure failure={launches.failure} />;
  const sources = await loadInsight("/api/v1/sources?limit=20", decodeSourceOverview);
  if (!sources.ok) return <InsightFailure failure={sources.failure} />;
  const compiler = await loadInsight("/api/v1/compiler/overview", decodeCompilerOverview);
  if (!compiler.ok) return <InsightFailure failure={compiler.failure} />;

  const events = composeHistory(launches.data, sources.data.snapshots, compiler.data.runs);

  return (
    <>
      <div className={shared.pageHeader}>
        <div>
          <h1 className={shared.pageTitle}>History</h1>
          <p className={shared.pageIntro}>
            Everything that has happened to this store in AgentRank, newest first.
          </p>
        </div>
      </div>
      {events.length === 0 ? (
        <Panel>
          <EmptyState
            title="Nothing has happened yet"
            explanation="Once you import your store and run an evaluation, every step appears here."
          >
            <Link className={shared.textLink} href="/overview">
              Start from your overview
            </Link>
          </EmptyState>
        </Panel>
      ) : (
        <ol className={styles.list} aria-label="Merchant history">
          {events.map((event) => (
            <EventRow key={`${event.kind}:${event.at}:${event.title}`} event={event} />
          ))}
        </ol>
      )}
      <p className={shared.finePrint}>
        The full run inventory with technical identifiers is in{" "}
        <Link className={shared.rowLink} href="/lab/runs">
          the Lab
        </Link>
        .
      </p>
    </>
  );
}

function EventRow({ event }: { event: MerchantEvent }) {
  return (
    <li className={styles.event}>
      <span className={styles.when}>{formatTimestamp(event.at)}</span>
      <div>
        <h2 className={styles.title}>
          {event.href === null ? event.title : <Link href={event.href}>{event.title}</Link>}
        </h2>
        <p className={styles.detail}>{event.detail}</p>
      </div>
      <StatusMark tone={event.status.tone} label={event.status.label} />
    </li>
  );
}
