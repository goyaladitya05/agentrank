import Link from "next/link";
import { redirect } from "next/navigation";

import { consoleCredential } from "@/lib/auth/credential";

import styles from "./entry.module.css";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "AgentRank",
  description: "Can AI shopping agents buy from your store?",
};

/** The loop the product walks a merchant through, in the order its pages show it. */
const LOOP = [
  { name: "Overview", body: "How many purchase scenarios AI agents completed against your store." },
  { name: "Issues", body: "What stopped the rest, and whether it is yours to fix." },
  { name: "Fixes", body: "Facts read from your own pages, one decision each." },
  { name: "Measure again", body: "The same scenarios, run against what you published." },
  { name: "Before and after", body: "What moved, with every caveat attached." },
] as const;

/** The four things every shopping attempt has to get through. */
const STAGES = [
  { stage: "Discover", body: "Find candidates at all, from what the store publishes." },
  { stage: "Understand", body: "Establish that a candidate meets what the shopper asked for." },
  { stage: "Select", body: "Choose one, and be right about it." },
  { stage: "Checkout", body: "Get through authorization and payment." },
] as const;

/**
 * The first thing somebody who has never seen AgentRank meets.
 *
 * A signed in merchant never sees it: they are working, and their overview is what they came
 * for. What this is for is the thirty seconds before anyone has explained anything, and it
 * makes exactly one argument, in the order the product makes it.
 *
 * Deliberately not a marketing page. It states what AgentRank does, names the loop a merchant
 * walks, names the four stages a shopping attempt passes through, and hands over to the real
 * product. Every number a visitor will see is behind sign in and comes from a real run; nothing
 * on this page asserts a result, because this page has no merchant and therefore no evidence.
 */
export default async function EntryPage() {
  if ((await consoleCredential()) !== null) {
    redirect("/overview");
  }
  return (
    <main className={styles.page}>
      <header className={styles.masthead}>
        <p className={styles.wordmark}>AgentRank</p>
        <h1 className={styles.headline}>
          Can AI shopping agents <em>buy</em> from your store?
        </h1>
        <p className={styles.lede}>
          AgentRank measures how well AI agents can actually shop from a merchant&apos;s store,
          shows exactly what stopped each purchase, lets the merchant review the fixes, and measures
          the result again.
        </p>
        <p className={styles.actions}>
          <Link className={styles.primary} href="/login">
            Sign in to the demo
          </Link>
          <Link className={styles.secondary} href="/lab">
            AgentRank Lab
          </Link>
        </p>
      </header>

      <section className={styles.loop} aria-label="How it works">
        <p className={styles.sectionLabel}>One loop, five screens</p>
        <ol className={styles.loopList}>
          {LOOP.map((entry, index) => (
            <li key={entry.name} className={styles.loopItem}>
              <span className={styles.loopIndex}>0{index + 1}</span>
              <span className={styles.loopName}>{entry.name}</span>
              <span className={styles.loopBody}>{entry.body}</span>
            </li>
          ))}
        </ol>
      </section>

      <section className={styles.stages} aria-label="What a shopping attempt has to do">
        <p className={styles.sectionLabel}>Every attempt has to get through four things</p>
        <ol className={styles.stageList}>
          {STAGES.map((entry) => (
            <li key={entry.stage} className={styles.stageItem}>
              <span className={styles.stageName}>{entry.stage}</span>
              <span className={styles.stageBody}>{entry.body}</span>
            </li>
          ))}
        </ol>
        <p className={styles.note}>
          AgentRank records the stage each attempt reached from its own trusted commerce records and
          the diagnosis its evaluator assigned. It never reads an agent&apos;s account of itself.
        </p>
      </section>

      <footer className={styles.footer}>
        <p>
          The demo merchant is fictional and its demand figures are simulated benchmark values,
          never revenue. Where a comparison is not strong enough to read, AgentRank says so instead
          of showing an improvement.
        </p>
        <p>
          <Link className={styles.primary} href="/login">
            Sign in to the demo
          </Link>
        </p>
      </footer>
    </main>
  );
}
