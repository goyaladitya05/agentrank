import { describe, expect, it } from "vitest";

import { DecodeError } from "@/lib/insights/decode";
import { decodeEvaluationSetup, decodeWorkspace } from "@/lib/workspace";

const CATALOG = {
  products: 3,
  variants: 5,
  purchasable_variants: 4,
  simulated_stock_variants: 2,
  assumed_stock_units: 3,
  currencies: ["INR"],
  categories: ["chargers"],
};

const WORKSPACE = {
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
  composition: [
    {
      family: "CATEGORY_PURCHASE",
      missions: 2,
      purchase_available: 2,
      no_acceptable_purchase: 0,
    },
  ],
  unsupported: [{ family: "POLICY_CONSTRAINT", reason: "Not markable." }],
  generator_version: "workspace-v1",
  configuration_digest: `sha256:${"a".repeat(64)}`,
  catalog_hash: `sha256:${"b".repeat(64)}`,
  suite_hash: `sha256:${"c".repeat(64)}`,
};

const SETUP = {
  buildable: false,
  current_source_snapshot_id: "01a03000-0000-7000-8000-0000000000aa",
  current_source_snapshot_label: "merchant-source@1",
  source_is_newer_than_the_workspace: false,
  workspace: WORKSPACE,
  operator_world_label: null,
  planned: null,
  blockers: [],
};

describe("decodeWorkspace", () => {
  it("reads a built setup field by field", () => {
    const decoded = decodeWorkspace(WORKSPACE);

    expect(decoded.suite_label).toBe("acme-workspace-suite@1");
    expect(decoded.mission_count).toBe(4);
    expect(decoded.catalog.purchasable_variants).toBe(4);
    expect(decoded.catalog.simulated_stock_variants).toBe(2);
    expect(decoded.catalog.assumed_stock_units).toBe(3);
    expect(decoded.composition[0]?.family).toBe("CATEGORY_PURCHASE");
    expect(decoded.unsupported[0]?.reason).toBe("Not markable.");
  });

  it("refuses a mission count that is not an integer", () => {
    expect(() => decodeWorkspace({ ...WORKSPACE, mission_count: "four" })).toThrow(DecodeError);
  });

  it("refuses a catalog that is missing a count", () => {
    expect(() =>
      decodeWorkspace({ ...WORKSPACE, catalog: { ...CATALOG, variants: null } }),
    ).toThrow(DecodeError);
  });

  it("accepts a mission kind this build has no label for", () => {
    // A setup built by an older generator stays readable rather than becoming undecodable.
    const decoded = decodeWorkspace({
      ...WORKSPACE,
      unsupported: [{ family: "SOMETHING_NEWER", reason: "Not supported." }],
    });

    expect(decoded.unsupported[0]?.family).toBe("SOMETHING_NEWER");
  });
});

describe("decodeEvaluationSetup", () => {
  it("reads a merchant who has a setup and no plan to build another", () => {
    const decoded = decodeEvaluationSetup(SETUP);

    expect(decoded.buildable).toBe(false);
    expect(decoded.workspace?.workspace_id).toBe(WORKSPACE.workspace_id);
    expect(decoded.planned).toBeNull();
  });

  it("reads a merchant with a plan and no setup", () => {
    const decoded = decodeEvaluationSetup({
      ...SETUP,
      buildable: true,
      workspace: null,
      planned: {
        mission_count: 4,
        catalog: CATALOG,
        composition: [],
        unsupported: [],
        omitted_fields: ["products[P1].merchant_metadata.bin"],
        mission_budget: 12,
      },
    });

    expect(decoded.workspace).toBeNull();
    expect(decoded.planned?.mission_count).toBe(4);
    expect(decoded.planned?.omitted_fields).toEqual(["products[P1].merchant_metadata.bin"]);
  });

  it("reads a merchant with no source at all", () => {
    const decoded = decodeEvaluationSetup({
      buildable: false,
      current_source_snapshot_id: null,
      current_source_snapshot_label: null,
      source_is_newer_than_the_workspace: false,
      workspace: null,
      operator_world_label: null,
      planned: null,
      blockers: [{ code: "merchant_source_unavailable", message: "Add your source." }],
    });

    expect(decoded.blockers[0]?.code).toBe("merchant_source_unavailable");
  });

  it("reads a merchant whose world an operator registered", () => {
    const decoded = decodeEvaluationSetup({
      ...SETUP,
      workspace: null,
      operator_world_label: "voltedge-catalog@2",
    });

    expect(decoded.workspace).toBeNull();
    expect(decoded.operator_world_label).toBe("voltedge-catalog@2");
  });

  it("refuses a blocker with no message", () => {
    expect(() =>
      decodeEvaluationSetup({ ...SETUP, blockers: [{ code: "x", message: null }] }),
    ).toThrow(DecodeError);
  });
});

describe("a setup built before a source could omit a stock quantity", () => {
  it("reads a null simulated count as an absence rather than as a zero", () => {
    const decoded = decodeWorkspace({
      ...WORKSPACE,
      catalog: { ...CATALOG, simulated_stock_variants: null, assumed_stock_units: null },
    });

    expect(decoded.catalog.simulated_stock_variants).toBeNull();
    expect(decoded.catalog.assumed_stock_units).toBeNull();
  });
});
