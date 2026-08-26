import { randomUUID } from "node:crypto";

import Link from "next/link";

import { EvaluationSetupPanel } from "@/components/EvaluationSetup";
import { InsightFailure } from "@/components/InsightFailure";
import { LaunchEvaluation } from "@/components/LaunchEvaluation";
import { EmptyState, KeyValueList, Panel, Section, StatusMark } from "@/components/Primitives";
import { TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import merchant from "@/components/merchant.module.css";
import { formatTimestamp } from "@/lib/format";
import { launchStatusLabel } from "@/lib/labels";
import { loadInsight } from "@/lib/insights/load";
import { requestEvaluation } from "@/lib/evaluation-actions";
import { decodeEvaluationSetup } from "@/lib/workspace";
import { buildEvaluationSetup } from "@/lib/workspace-actions";
import {
  decodePreflight,
  decodeEvaluationLaunchList,
  type EvaluationLaunch,
  type EvaluationPreflight,
} from "@/lib/evaluation";

export const dynamic = "force-dynamic";
export const metadata = { title: "Measure | AgentRank" };

/**
 * The one page a merchant starts an evaluation from, whichever of the two commands they are
 * making.
 *
 * Which one that is comes from the server. A merchant who has published fixes is measuring
 * that published description; a merchant with nothing published and nothing measured is
 * asking how well an agent does against their store as it is. Every heading, every sentence
 * and the confirmation itself follow that answer rather than guessing from whichever fields
 * came back filled.
 *
 * The preflight leads with the four things worth knowing before spending: how many shopping
 * scenarios run, which model runs them, the model request allowance with the retry rule, and
 * what the result will be read against. The frozen identities behind the launch stay in a
 * technical disclosure.
 */
export default async function EvaluationsPage() {
  const setup = await loadInsight("/api/v1/benchmark/workspace", decodeEvaluationSetup);
  if (!setup.ok) return <InsightFailure failure={setup.failure} />;
  const preflight = await loadInsight("/api/v1/benchmark/evaluations/preflight", decodePreflight);
  if (!preflight.ok) return <InsightFailure failure={preflight.failure} />;
  const history = await loadInsight("/api/v1/benchmark/evaluations?limit=20", (value) =>
    decodeEvaluationLaunchList(value),
  );
  if (!history.ok) return <InsightFailure failure={history.failure} />;

  // One rendered form is one launch. The key is generated here, so submitting this form twice
  // or retrying after a lost response is the same request and produces the same launch; opening
  // the page again is a new key and therefore a deliberate second one.
  const requestKey = randomUUID();
  const initial = preflight.data.purpose === "INITIAL";
  return (
    <>
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>
            {initial ? "Run your first evaluation" : "Measure again"}
          </h1>
          <p className={merchant.pageIntro}>
            {initial
              ? "AI shopping agents attempt realistic purchase scenarios against your store, and AgentRank records exactly what happens."
              : "Your published fixes are measured with the same shopping scenarios, so the result reads as a before and after."}
          </p>
        </div>
      </div>
      <Section
        title="What AgentRank tests"
        hint="Built from your own store information. Building it spends nothing."
      >
        <EvaluationSetupPanel
          setup={setup.data}
          action={buildEvaluationSetup.bind(null, setup.data.current_source_snapshot_id ?? "")}
        />
      </Section>
      <Section
        title={initial ? "Run the evaluation" : "Run the re-evaluation"}
        hint={
          initial
            ? "This creates your first result."
            : "Publishing fixes never starts an evaluation. This does."
        }
      >
        <Panel>
          <Preflight
            preflight={preflight.data}
            action={requestEvaluation.bind(
              null,
              preflight.data.purpose,
              preflight.data.representation_id,
              requestKey,
              preflight.data.plan_digest,
            )}
          />
        </Panel>
      </Section>
      <Section title="Your evaluations" hint="Newest first.">
        <History launches={history.data} />
      </Section>
    </>
  );
}

function Preflight({
  preflight,
  action,
}: {
  preflight: EvaluationPreflight;
  action: Parameters<typeof LaunchEvaluation>[0]["action"];
}) {
  return (
    <>
      <KeyValueList
        entries={[
          {
            term: "Shopping scenarios",
            value:
              preflight.mission_count === null
                ? "unknown"
                : `${String(preflight.mission_count)}, one at a time`,
          },
          { term: "Model", value: buyerSentence(preflight) },
          {
            term: "Model requests",
            value:
              preflight.max_provider_requests === null
                ? "None. The reference buyer calls no model provider."
                : `At most ${String(preflight.max_provider_requests)}. Retries count against this allowance.`,
          },
          { term: "Compared against", value: comparisonSentence(preflight) },
        ]}
      />
      <TechnicalDetails summary="Technical details">
        <KeyValueList
          entries={[
            preflight.purpose === "INITIAL"
              ? {
                  term: "What is evaluated",
                  value: "Your merchant as it is now, through the ordinary storefront",
                }
              : {
                  term: "Representation under test",
                  value: preflight.representation_label ?? "None published",
                },
            { term: "Merchant information", value: informationSentence(preflight) },
            { term: "Benchmark suite", value: preflight.suite_label ?? "None published" },
            { term: "Benchmark world", value: preflight.environment_label ?? "Not registered" },
          ]}
        />
      </TechnicalDetails>
      {launchable(preflight) ? (
        <LaunchEvaluation preflight={preflight} action={action} />
      ) : (
        <Blockers preflight={preflight} />
      )}
    </>
  );
}

/**
 * Which of the merchant's own documents the buyer reads, in the vocabulary of the command.
 *
 * A first evaluation names the snapshot itself, because that snapshot is what is being measured.
 * A re-evaluation names the artifact instead: its source is the provenance of the representation
 * under test rather than the thing under test, and the representation's own label already
 * identifies it.
 */
function informationSentence(preflight: EvaluationPreflight): string {
  if (preflight.purpose === "INITIAL") {
    const label = preflight.source_snapshot_label ?? "Not recorded yet";
    return preflight.source_is_newer_than_the_setup
      ? `${label}, which your evaluation setup was built from. You have newer merchant information; build a new evaluation setup to measure that instead.`
      : label;
  }
  return preflight.representation_id === null
    ? "None published"
    : "From the representation under test";
}

/**
 * Whether the confirmation is reachable at all.
 *
 * The server's own answer, plus the one thing the form itself needs: a re-evaluation names the
 * representation it measures, so a launchable plan that resolved none would render a button
 * that could only be refused.
 */
function launchable(preflight: EvaluationPreflight): boolean {
  if (!preflight.launchable) return false;
  return preflight.purpose === "INITIAL" || preflight.representation_id !== null;
}

/**
 * What this result will be read against, stated as the absence it is when there is none.
 *
 * Never a zero, never a percentage, and never "no change". A merchant with no earlier run has
 * no before, and the only honest thing to publish is that sentence.
 */
function comparisonSentence(preflight: EvaluationPreflight): string {
  if (preflight.purpose === "INITIAL") {
    return "Nothing. This is your first evaluation, so there is no earlier result to read it against.";
  }
  if (preflight.baseline_run_id === null) {
    return "No earlier completed run of this suite";
  }
  if (preflight.baseline_surface_matches === false) {
    return "Nothing. Your most recent completed run of this suite measured a different kind of surface, so the two are not read as a before and after.";
  }
  return `Your run completed ${formatTimestamp(preflight.baseline_run_completed_at)}`;
}

function Blockers({ preflight }: { preflight: EvaluationPreflight }) {
  const initial = preflight.purpose === "INITIAL";
  return (
    <>
      <p>
        {initial
          ? "A first evaluation cannot be run yet."
          : "A re-evaluation cannot be requested right now."}
      </p>
      <ul className={styles.launchTerms}>
        {preflight.blockers.map((blocker) => (
          <li key={blocker.code}>
            {blocker.message} <BlockerAction code={blocker.code} />
          </li>
        ))}
      </ul>
      {preflight.pending_launch_id === null ? null : (
        // "Open the evaluation already in progress" was wrong for the case a merchant most often
        // lands here in: a launch waiting for a worker nobody has configured is not in progress,
        // waiting will not end it, and the page it links to is the one place it can be put down.
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/evaluations/${encodeURIComponent(preflight.pending_launch_id)}`}
          >
            Open the evaluation you already asked for
          </Link>
          . A queued one that has not started can be withdrawn there.
        </p>
      )}
    </>
  );
}

/**
 * The one place in the console a blocker becomes a link.
 *
 * Only for the blockers a merchant can actually clear themselves. Everything else is an
 * operator's job and a link would suggest otherwise.
 *
 * The two benchmark setup blockers became merchant-clearable in Phase 5C: a merchant with
 * source evidence and no evaluation world builds one from the panel at the top of this page,
 * so they are pointed there rather than told their operator runs a command line.
 */
function BlockerAction({ code }: { code: string }) {
  if (code === "merchant_source_unavailable") {
    return (
      <Link className={styles.rowLink} href="/sources/new">
        Add your merchant source
      </Link>
    );
  }
  if (code === "no_published_representation") {
    return (
      <Link className={styles.rowLink} href="/fixes">
        Review and publish your fixes
      </Link>
    );
  }
  if (code === "benchmark_suite_unavailable" || code === "benchmark_world_unregistered") {
    return <span className={styles.finePrint}>See what AgentRank tests above.</span>;
  }
  return null;
}

function buyerSentence(preflight: EvaluationPreflight): string {
  if (preflight.buyer_profile === "AI_BUYER") {
    return `${preflight.provider ?? "model provider"}, requested model ${preflight.requested_model ?? "unrecorded"}`;
  }
  return "AgentRank's deterministic reference buyer, which is not an AI agent";
}

/** What one launch measured, in merchant words. The exact artifact is on its detail page. */
function measured(launch: EvaluationLaunch): string {
  if (launch.purpose === "INITIAL") {
    return launch.source_snapshot_label ?? "Your merchant information";
  }
  return "Your published fixes";
}

function History({ launches }: { launches: readonly EvaluationLaunch[] }) {
  if (launches.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No evaluations have run yet"
          explanation="When you request one, it appears here with what it froze and what became of it."
        />
      </Panel>
    );
  }
  return (
    <div className={styles.tableScroll} tabIndex={0} aria-label="Evaluations">
      <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">Requested</th>
            <th scope="col">Kind</th>
            <th scope="col">Measured</th>
            <th scope="col">State</th>
            <th scope="col">Progress</th>
          </tr>
        </thead>
        <tbody>
          {launches.map((launch) => {
            const status = launchStatusLabel(launch.status, launch.failure_code);
            return (
              <tr key={launch.launch_id}>
                <td>
                  <Link
                    className={styles.rowLinkStrong}
                    href={`/evaluations/${encodeURIComponent(launch.launch_id)}`}
                  >
                    {formatTimestamp(launch.requested_at)}
                  </Link>
                </td>
                <td>{launch.purpose === "INITIAL" ? "First evaluation" : "Re-evaluation"}</td>
                <td>{measured(launch)}</td>
                <td>
                  <StatusMark tone={status.tone} label={status.label} />
                </td>
                <td>
                  {launch.missions_completed === null
                    ? "not started"
                    : `${String(launch.missions_completed)} of ${String(launch.mission_count)} scenarios`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
