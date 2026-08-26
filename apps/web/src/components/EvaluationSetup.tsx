"use client";

import { useActionState } from "react";

import { KeyValueList, Panel, StatusMark } from "@/components/Primitives";
import { IdRow, TechnicalDetails } from "@/components/TechnicalDetails";
import styles from "@/components/console.module.css";
import { missionFamilyLabel } from "@/lib/labels";
import type { EvaluationCatalogSummary, EvaluationSetup, PlannedWorkspace } from "@/lib/workspace";
import { IDLE_SETUP, type SetupState } from "@/lib/workspace-mutation";

/**
 * What AgentRank will measure this merchant against, and the command that creates it.
 *
 * This is the panel that used to be missing. A merchant with source evidence and no evaluation
 * world was told, in the launch preflight, that their operator publishes a benchmark suite from
 * a command line, which is not something a merchant can act on and was the last place a real
 * merchant needed a developer.
 *
 * What it says is deliberately factual. Building a setup is deterministic, spends nothing and
 * changes no price, stock level or payment, so it is a single button rather than the two step
 * confirmation a launch gets. What it produces is stated as counts and composition before the
 * merchant presses it, so the size of the benchmark they will later pay a model provider to run
 * is known in advance rather than discovered.
 *
 * Split from the page so that every state a merchant can land in, including a refusal and a lost
 * response, is renderable in a test without driving a browser.
 */

export type SetupAction = (state: SetupState) => SetupState | Promise<SetupState>;

export function EvaluationSetupPanel({
  setup,
  action,
}: {
  setup: EvaluationSetup;
  action: SetupAction;
}) {
  if (setup.workspace !== null) {
    return <BuiltSetup setup={setup} action={action} />;
  }
  // A merchant an operator registered from authored files. They are evaluable, and reporting
  // that as a blocked setup would be telling a working merchant something is wrong with them.
  if (setup.operator_world_label !== null) {
    return <OperatorSetup label={setup.operator_world_label} />;
  }
  if (setup.planned !== null && setup.buildable) {
    return <BuildableSetup setup={setup} action={action} />;
  }
  return <BlockedSetup setup={setup} />;
}

/** A merchant whose evaluation world AgentRank's operator prepared rather than this command. */
function OperatorSetup({ label }: { label: string }) {
  return (
    <Panel>
      <div className={styles.panelHead}>
        <StatusMark
          tone="ok"
          label="Ready"
          description="This merchant has an evaluation world an operator prepared"
        />
      </div>
      <p>
        AgentRank&apos;s operator prepared the evaluation catalog and benchmark missions for this
        merchant. They are used exactly as a generated setup would be, and nothing here replaces
        them.
      </p>
      <KeyValueList entries={[{ term: "Evaluation catalog", value: label }]} />
    </Panel>
  );
}

/**
 * A merchant who is set up, and the one fact that might make them want a second setup.
 *
 * The composition is the honest part. A merchant reading "12 missions" learns nothing about
 * whether the benchmark asks them to sell anything hard, and a suite that turned out to be
 * twelve easy purchases should look different from one with correct-abstention cases in it.
 */
function BuiltSetup({ setup, action }: { setup: EvaluationSetup; action: SetupAction }) {
  const workspace = setup.workspace;
  if (workspace === null) return null;
  return (
    <Panel>
      <div className={styles.panelHead}>
        <StatusMark tone="ok" label="Ready" description="This merchant can be evaluated" />
      </div>
      <KeyValueList
        entries={[
          { term: "Built from", value: workspace.source_snapshot_label },
          {
            term: "Evaluation catalog",
            value: `${String(workspace.catalog.products)} products, ${String(workspace.catalog.purchasable_variants)} of ${String(workspace.catalog.variants)} variants in stock`,
          },
          { term: "Simulated stock", value: simulatedStock(workspace.catalog) },
          {
            term: "Currencies",
            value:
              workspace.catalog.currencies.length === 0
                ? "none recorded"
                : workspace.catalog.currencies.join(" · "),
          },
          { term: "Benchmark suite", value: workspace.suite_label },
          { term: "Missions", value: String(workspace.mission_count) },
        ]}
      />
      <Composition
        composition={workspace.composition}
        unsupported={workspace.unsupported}
        omitted={[]}
      />
      {setup.source_is_newer_than_the_workspace ? (
        <NewerSource setup={setup} action={action} />
      ) : null}
      {setup.blockers.length === 0 ? null : (
        <ul className={styles.launchTerms}>
          {setup.blockers.map((blocker) => (
            <li key={blocker.code}>{blocker.message}</li>
          ))}
        </ul>
      )}
      <TechnicalDetails summary="Evaluation setup identifiers">
        <IdRow label="Workspace id" value={workspace.workspace_id} />
        <IdRow label="Evaluation catalog" value={workspace.environment_label} />
        <IdRow label="Catalog digest" value={workspace.catalog_hash} />
        <IdRow label="Suite digest" value={workspace.suite_hash} />
        <IdRow label="Generator" value={workspace.generator_version} />
        <IdRow label="Generation digest" value={workspace.configuration_digest} />
      </TechnicalDetails>
    </Panel>
  );
}

/**
 * Newer evidence, stated as an offer rather than as a problem.
 *
 * Nothing is rebuilt and no earlier result is invalidated by a newer snapshot. What a merchant
 * can do is build a second setup, and every run they have already has stays pointed at exactly
 * the world and workload it executed.
 */
function NewerSource({ setup, action }: { setup: EvaluationSetup; action: SetupAction }) {
  return (
    <div className={styles.finePrintTight}>
      <p>
        Newer merchant information is available
        {setup.current_source_snapshot_label === null
          ? ""
          : ` (${setup.current_source_snapshot_label})`}
        . Your current setup was built from earlier evidence and stays exactly as it is; every
        evaluation you have already run keeps measuring what it measured.
      </p>
      {setup.buildable && setup.planned !== null ? (
        <BuildForm planned={setup.planned} action={action} label="Build a new evaluation setup" />
      ) : null}
    </div>
  );
}

/** A merchant with source evidence and no setup yet: the whole reason this panel exists. */
function BuildableSetup({ setup, action }: { setup: EvaluationSetup; action: SetupAction }) {
  const planned = setup.planned;
  if (planned === null) return null;
  return (
    <Panel>
      <div className={styles.panelHead}>
        <StatusMark
          tone="warn"
          label="Setup needed"
          description="AgentRank has your merchant information and no evaluation setup yet"
        />
      </div>
      <p>
        AgentRank builds the isolated catalog and the benchmark missions it will evaluate you
        against from your own merchant information. Nothing is sent to a model and no evaluation
        runs.
      </p>
      <KeyValueList
        entries={[
          { term: "Built from", value: setup.current_source_snapshot_label ?? "your source" },
          {
            term: "Evaluation catalog",
            value: `${String(planned.catalog.products)} products, ${String(planned.catalog.purchasable_variants)} of ${String(planned.catalog.variants)} variants in stock`,
          },
          { term: "Simulated stock", value: simulatedStock(planned.catalog) },
          { term: "Missions", value: String(planned.mission_count) },
        ]}
      />
      <Composition
        composition={planned.composition}
        unsupported={planned.unsupported}
        omitted={planned.omitted_fields}
      />
      <BuildForm planned={planned} action={action} label="Prepare evaluation setup" />
    </Panel>
  );
}

/**
 * How much of this evaluation world's stock is a simulation rather than the merchant's evidence.
 *
 * Said plainly and in both directions. A merchant whose pages all publish counts reads that
 * nothing was assumed, which is worth stating rather than leaving as an absent row; a merchant
 * whose pages publish only "In stock" reads exactly how many of their lines got a depth from
 * AgentRank and what that depth is.
 */
function simulatedStock(catalog: EvaluationCatalogSummary): string {
  const { simulated_stock_variants: simulated, assumed_stock_units: units } = catalog;
  if (simulated === null || units === null) return "Not recorded for this setup";
  if (simulated === 0) return "None. Every stock level came from your own merchant information";
  return `${String(simulated)} of ${String(catalog.variants)} variants hold ${String(units)} units, because your merchant information says they are in stock without saying how many. This is an evaluation assumption and not your stock`;
}

/** A merchant who cannot be set up, told exactly which fact about their evidence stops it. */
function BlockedSetup({ setup }: { setup: EvaluationSetup }) {
  return (
    <Panel>
      <div className={styles.panelHead}>
        <StatusMark
          tone="warn"
          label="Setup blocked"
          description="AgentRank cannot build an evaluation setup from this merchant yet"
        />
      </div>
      {setup.blockers.length === 0 ? (
        <p>AgentRank cannot build an evaluation setup for this merchant right now.</p>
      ) : (
        <ul className={styles.launchTerms}>
          {setup.blockers.map((blocker) => (
            <li key={blocker.code}>{blocker.message}</li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function BuildForm({
  planned,
  action,
  label,
}: {
  planned: PlannedWorkspace;
  action: SetupAction;
  label: string;
}) {
  const [state, formAction, pending] = useActionState(action, IDLE_SETUP);
  if (state.ok) {
    return (
      <div role="status">
        <p>
          Evaluation setup ready.
          {state.missionCount === null
            ? ""
            : ` ${String(state.missionCount)} benchmark missions were prepared.`}{" "}
          Nothing has been evaluated yet.
        </p>
      </div>
    );
  }
  return (
    <form action={formAction} aria-label="Build evaluation setup">
      <ul className={styles.launchTerms}>
        <li>
          {String(planned.mission_count)} benchmark missions are prepared from your merchant
          information. AgentRank does not add products, prices or specifications you did not supply.
        </li>
        <li>
          This changes no price, no stock level and no payment. It creates the isolated catalog
          AgentRank evaluates against, which is separate from anything you sell elsewhere.
        </li>
        <li>
          No model provider is contacted and nothing is spent. Running the evaluation is a separate
          step you ask for.
        </li>
        <li>
          Your setup is kept as it was built. Submitting newer merchant information later never
          changes it and never changes an evaluation you have already run.
        </li>
      </ul>
      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          {label}
        </button>
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Preparing your evaluation setup
        </p>
      ) : null}
      {state.message !== null && !pending ? (
        <p className={styles.mutationAlert} role="alert">
          {state.message}
          {state.stale && !state.unknown ? " The state shown here is current." : ""}
        </p>
      ) : null}
    </form>
  );
}

/**
 * What kinds of mission the suite holds, and which kinds this merchant's data could not support.
 *
 * The second half is the one worth having. A suite of four straightforward purchases and a suite
 * whose merchant has nothing out of stock and no structured specifications look identical
 * without it, and only one of those is a fact about the merchant.
 */
export function Composition({
  composition,
  unsupported,
  omitted,
}: {
  composition: readonly {
    readonly family: string;
    readonly missions: number;
    readonly purchase_available: number;
    readonly no_acceptable_purchase: number;
  }[];
  unsupported: readonly { readonly family: string; readonly reason: string }[];
  omitted: readonly string[];
}) {
  return (
    <>
      {composition.length === 0 ? null : (
        <div className={styles.tableScroll} tabIndex={0} aria-label="Mission composition">
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">Mission kind</th>
                <th scope="col" className={styles.num}>
                  Missions
                </th>
                <th scope="col" className={styles.num}>
                  Buy
                </th>
                <th scope="col" className={styles.num}>
                  Decline
                </th>
              </tr>
            </thead>
            <tbody>
              {composition.map((entry) => (
                <tr key={entry.family}>
                  <td>{missionFamilyLabel(entry.family)}</td>
                  <td className={styles.num}>{String(entry.missions)}</td>
                  <td className={styles.num}>{String(entry.purchase_available)}</td>
                  <td className={styles.num}>{String(entry.no_acceptable_purchase)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {unsupported.length === 0 ? null : (
        <TechnicalDetails
          summary={`Mission kinds your data does not support (${String(unsupported.length)})`}
        >
          {unsupported.map((entry) => (
            <p key={entry.family} className={styles.finePrintTight}>
              <span className={styles.monoMuted}>{missionFamilyLabel(entry.family)}</span>{" "}
              {entry.reason}
            </p>
          ))}
        </TechnicalDetails>
      )}
      {omitted.length === 0 ? null : (
        <TechnicalDetails
          summary={`Fields not carried into the evaluation catalog (${String(omitted.length)})`}
        >
          {omitted.map((field) => (
            <p key={field} className={styles.finePrintTight}>
              <span className={styles.monoMuted}>{field}</span>
            </p>
          ))}
        </TechnicalDetails>
      )}
    </>
  );
}
