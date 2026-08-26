import Link from "next/link";
import { redirect } from "next/navigation";

import { consoleCredential } from "@/lib/auth/credential";

import styles from "./entry.module.css";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "AgentRank",
  description: "Can AI shopping agents buy from your store?",
};

/**
 * The first thing somebody who has never seen AgentRank meets.
 *
 * A signed in merchant never sees it: they are working, and their overview is what they came
 * for. What this is for is the thirty seconds before anyone has explained anything, and it
 * makes exactly one argument, in the order the product makes it.
 *
 * Deliberately not a marketing page. It states what AgentRank does, names the four stages a
 * shopping attempt passes through, and hands over to the real product. Every number a visitor
 * will see is behind sign in and comes from a real run; nothing on this page asserts a result,
 * because this page has no merchant and therefore no evidence.
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
          AgentRank runs shopping agents through realistic purchase scenarios against a real
          merchant, records exactly where each attempt stopped, and separates what the merchant can
          fix from what is not their problem.
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

      <section className={styles.stages} aria-label="What a shopping attempt has to do">
        <p className={styles.sectionLabel}>Every attempt has to get through four things</p>
        <ol className={styles.stageList}>
          {[
            {
              stage: "Discover",
              body: "Find candidates at all, from what the store publishes.",
            },
            {
              stage: "Understand",
              body: "Establish that a candidate meets what the shopper actually asked for.",
            },
            { stage: "Select", body: "Choose one, and be right about it." },
            { stage: "Checkout", body: "Get through authorization and payment." },
          ].map((entry, index) => (
            <li key={entry.stage} className={styles.stageItem}>
              <span className={styles.stageIndex}>0{index + 1}</span>
              <span className={styles.stageName}>{entry.stage}</span>
              <span className={styles.stageBody}>{entry.body}</span>
            </li>
          ))}
        </ol>
        <p className={styles.note}>
          AgentRank records the stage each attempt reached from its own trusted commerce artifacts
          and the diagnosis its evaluator assigned. It never reads an agent&apos;s account of
          itself.
        </p>
      </section>

      <section className={styles.loop} aria-label="What the product does with that">
        <p className={styles.sectionLabel}>What a merchant does with it</p>
        <ol className={styles.loopList}>
          {[
            ["See which scenarios failed", "and which stage each one stopped at."],
            ["Read why", "with the evidence AgentRank based it on."],
            [
              "Review the facts it recovered",
              "from the merchant's own pages, one decision at a time.",
            ],
            ["Publish them", "as the description shopping agents read."],
            ["Measure again", "against the same scenarios, and read the difference honestly."],
          ].map(([title, rest], index) => (
            <li key={title} className={styles.loopItem}>
              <span className={styles.stageIndex}>0{index + 1}</span>
              <span>
                <strong>{title}</strong> {rest}
              </span>
            </li>
          ))}
        </ol>
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
