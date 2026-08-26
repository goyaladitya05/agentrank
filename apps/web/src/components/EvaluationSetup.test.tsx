import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { EvaluationSetupPanel } from "./EvaluationSetup";
import type { EvaluationSetup, EvaluationWorkspace, PlannedWorkspace } from "@/lib/workspace";
import { IDLE_SETUP } from "@/lib/workspace-mutation";

const CATALOG = {
  products: 3,
  variants: 5,
  purchasable_variants: 4,
  simulated_stock_variants: 0,
  assumed_stock_units: 3,
  currencies: ["INR"],
  categories: ["cables", "chargers"],
} as const;

const COMPOSITION = [
  {
    family: "CATEGORY_PURCHASE",
    missions: 2,
    purchase_available: 2,
    no_acceptable_purchase: 0,
  },
  {
    family: "BUDGET_ABSTENTION",
    missions: 2,
    purchase_available: 0,
    no_acceptable_purchase: 2,
  },
] as const;

const UNSUPPORTED = [
  {
    family: "POLICY_CONSTRAINT",
    reason:
      "A mission is marked on whether a purchase was available, so an answer to a policy question is not something this benchmark can mark.",
  },
] as const;

const PLANNED: PlannedWorkspace = {
  mission_count: 4,
  catalog: CATALOG,
  composition: COMPOSITION,
  unsupported: UNSUPPORTED,
  omitted_fields: ["products[P1].variants[P1-A].merchant_metadata.cable_length_m"],
  mission_budget: 12,
};

const WORKSPACE: EvaluationWorkspace = {
  workspace_id: "01a03000-0000-7000-8000-000000000001",
  created_at: "2026-08-25T12:00:00Z",
  source_snapshot_id: "01a03000-0000-7000-8000-0000000000aa",
  source_snapshot_label: "merchant-source@1",
  environment_id: "01a03000-0000-7000-8000-0000000000bb",
  environment_label: "acme-workspace-catalog@1",
  suite_id: "01a03000-0000-7000-8000-0000000000cc",
  suite_label: "acme-workspace-suite@1",
  mission_count: 4,
  catalog: CATALOG,
  composition: COMPOSITION,
  unsupported: UNSUPPORTED,
  generator_version: "workspace-v1",
  configuration_digest: `sha256:${"a".repeat(64)}`,
  catalog_hash: `sha256:${"b".repeat(64)}`,
  suite_hash: `sha256:${"c".repeat(64)}`,
};

function setup(overrides: Partial<EvaluationSetup> = {}): EvaluationSetup {
  return {
    buildable: true,
    current_source_snapshot_id: WORKSPACE.source_snapshot_id,
    current_source_snapshot_label: "merchant-source@1",
    source_is_newer_than_the_workspace: false,
    workspace: null,
    operator_world_label: null,
    planned: PLANNED,
    blockers: [],
    ...overrides,
  };
}

function render(value: EvaluationSetup): string {
  return renderToStaticMarkup(
    <EvaluationSetupPanel setup={value} action={async () => IDLE_SETUP} />,
  );
}

describe("<EvaluationSetupPanel>", () => {
  it("offers a merchant with source evidence and no setup a way to build one", () => {
    const html = render(setup());

    expect(html).toContain("Setup needed");
    expect(html).toContain("Prepare evaluation setup");
    expect(html).toContain("merchant-source@1");
  });

  it("states the mission count and composition before anything is built", () => {
    const html = render(setup());

    expect(html).toContain("4");
    expect(html).toContain("Buy something from a category");
    expect(html).toContain("Decline when nothing is affordable");
  });

  it("says that building spends nothing and changes no commerce state", () => {
    const html = render(setup());

    expect(html).toContain("changes no price, no stock level and no payment");
    expect(html).toContain("No model provider is contacted and nothing is spent");
  });

  it("names the mission kinds the merchant's own data cannot support", () => {
    const html = render(setup());

    expect(html).toContain("Answer a policy question");
    expect(html).toContain("not something this benchmark can mark");
  });

  it("names the source fields the evaluation catalog does not carry", () => {
    const html = render(setup());

    expect(html).toContain("merchant_metadata.cable_length_m");
  });

  it("reports a built setup as ready rather than offering to build a second", () => {
    const html = render(setup({ workspace: WORKSPACE, planned: null, buildable: false }));

    expect(html).toContain("Ready");
    expect(html).toContain("acme-workspace-suite@1");
    expect(html).not.toContain("Prepare evaluation setup");
  });

  it("reports newer evidence as an offer and says the existing setup is unchanged", () => {
    const html = render(
      setup({
        workspace: WORKSPACE,
        source_is_newer_than_the_workspace: true,
        current_source_snapshot_label: "merchant-source@2",
      }),
    );

    expect(html).toContain("Newer merchant information is available");
    expect(html).toContain("stays exactly as it is");
    expect(html).toContain("Build a new evaluation setup");
  });

  it("shows a merchant with no source what stops the setup rather than a generic failure", () => {
    const html = render(
      setup({
        buildable: false,
        planned: null,
        current_source_snapshot_id: null,
        current_source_snapshot_label: null,
        blockers: [
          {
            code: "merchant_source_unavailable",
            message:
              "AgentRank has no record of your merchant information yet, so there is nothing to build an evaluation setup from. Add your merchant source first.",
          },
        ],
      }),
    );

    expect(html).toContain("Setup blocked");
    expect(html).toContain("Add your merchant source first");
    expect(html).not.toContain("Prepare evaluation setup");
  });

  it("reports an operator prepared world as ready rather than as a blocked setup", () => {
    // A merchant an operator registered from authored files is evaluable, and telling them
    // their setup is blocked would be telling a working merchant something is wrong.
    const html = render(
      setup({
        buildable: false,
        planned: null,
        operator_world_label: "voltedge-catalog@2",
        blockers: [
          {
            code: "existing_benchmark_world",
            message: "This merchant already has a benchmark world AgentRank did not generate.",
          },
        ],
      }),
    );

    expect(html).toContain("Ready");
    expect(html).toContain("voltedge-catalog@2");
    expect(html).not.toContain("Setup blocked");
    expect(html).not.toContain("Prepare evaluation setup");
  });

  it("says why a built setup cannot be rebuilt while an evaluation is running", () => {
    const html = render(
      setup({
        workspace: WORKSPACE,
        buildable: false,
        planned: null,
        source_is_newer_than_the_workspace: true,
        current_source_snapshot_label: "merchant-source@2",
        blockers: [
          {
            code: "evaluation_already_pending",
            message:
              "A new evaluation setup cannot be built while an evaluation is queued or running.",
          },
        ],
      }),
    );

    expect(html).toContain("Newer merchant information is available");
    expect(html).toContain("cannot be built while an evaluation is queued or running");
    expect(html).not.toContain("Build a new evaluation setup");
  });

  it("never renders a mission objective or its expected outcome", () => {
    const built = render(setup({ workspace: WORKSPACE }));
    const planned = render(setup());

    for (const html of [built, planned]) {
      expect(html).not.toContain("PURCHASE_AVAILABLE");
      expect(html).not.toContain("NO_ACCEPTABLE_PURCHASE");
      expect(html).not.toContain("objective");
    }
  });

  it("avoids growth language and states what the setup is for", () => {
    const html = render(setup());

    for (const forbidden of ["Unlock", "AI readiness", "Optimize", "score"]) {
      expect(html).not.toContain(forbidden);
    }
  });
});

describe("<EvaluationSetupPanel> simulated stock", () => {
  it("says plainly when no stock level was assumed", () => {
    const html = render(setup({ workspace: WORKSPACE, planned: null, buildable: false }));

    expect(html).toContain("Every stock level came from your own merchant information");
  });

  it("reports an assumed depth as an assumption rather than as the merchant's stock", () => {
    const assumed: EvaluationWorkspace = {
      ...WORKSPACE,
      catalog: { ...CATALOG, simulated_stock_variants: 4, assumed_stock_units: 3 },
    };
    const html = render(setup({ workspace: assumed, planned: null, buildable: false }));

    expect(html).toContain("4 of 5 variants hold 3 units");
    expect(html).toContain("This is an evaluation assumption and not your stock");
  });

  it("says nothing was recorded rather than nothing was assumed", () => {
    const older: EvaluationWorkspace = {
      ...WORKSPACE,
      catalog: { ...CATALOG, simulated_stock_variants: null, assumed_stock_units: null },
    };
    const html = render(setup({ workspace: older, planned: null, buildable: false }));

    expect(html).toContain("Not recorded for this setup");
  });
});
