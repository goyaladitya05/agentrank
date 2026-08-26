import Link from "next/link";

import { Panel, Section } from "@/components/Primitives";
import styles from "@/components/console.module.css";

export const metadata = { title: "Lab | AgentRank" };

interface LabEntry {
  readonly href: string;
  readonly title: string;
  readonly body: string;
}

const SURFACES: readonly LabEntry[] = [
  {
    href: "/lab/runs",
    title: "Benchmark runs",
    body: "Every run with its designation, metrics, findings, missions and per-mission traces.",
  },
  {
    href: "/lab/compiler",
    title: "Compiler runs",
    body: "Compilation history over source snapshots: review state, configuration digests and publication lineage.",
  },
  {
    href: "/sources",
    title: "Sources and snapshots",
    body: "Immutable source evidence, import records, content hashes and the compiler runs over each snapshot.",
  },
  {
    href: "/evaluations",
    title: "Evaluation launches",
    body: "What each launch froze, its provider request allowance and spending, and the run it produced.",
  },
  {
    href: "/lab/status",
    title: "System status",
    body: "API, database and schema health as this console reads them right now.",
  },
];

/**
 * The Lab directory. Some entries point at merchant routes on purpose: a source snapshot or
 * an evaluation launch is one page whichever door it is entered through, and duplicating it
 * here would be two pages to keep truthful about one artifact.
 */
export default function LabIndexPage() {
  return (
    <>
      <div className={styles.pageHeader}>
        <h1 className={styles.pageTitle}>AgentRank Lab</h1>
      </div>
      <Section title="What this is">
        <Panel>
          <p>
            The Lab is the technical view of the same evidence the merchant product presents:
            benchmark runs, mission traces, compiler internals, configuration identities and
            methodology detail. Everything here is scoped to your merchant. Nothing here is required
            to complete the merchant workflow.
          </p>
        </Panel>
      </Section>
      <Section title="Surfaces">
        <div className={styles.panel}>
          {SURFACES.map((entry) => (
            <article key={entry.href} className={styles.finding}>
              <h2 className={styles.findingTitle}>
                <Link className={styles.rowLinkStrong} href={entry.href}>
                  {entry.title}
                </Link>
              </h2>
              <p className={styles.reviewMeta}>{entry.body}</p>
            </article>
          ))}
        </div>
      </Section>
      <Section title="Reached from evidence">
        <Panel>
          <p className={styles.reviewMeta}>
            Mission traces are under each benchmark run. Controlled experiment comparisons are
            linked from the merchant overview and from the runs they aggregate; each lives at{" "}
            <span className={styles.mono}>/lab/experiments/&lt;id&gt;</span>.
          </p>
        </Panel>
      </Section>
    </>
  );
}
