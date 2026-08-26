/**
 * The shapes the evaluation setup API returns, restated for the console and validated by hand.
 *
 * The same rule the evaluation and insights decoders follow: these values decide what a merchant
 * is told about what AgentRank will measure them on, so nothing is cast and every field is
 * checked by name. Wire names are kept verbatim, which makes a backend contract change a diff
 * here rather than an undefined property in a panel.
 *
 * There is deliberately no mission here and no expected outcome. The API publishes counts and
 * composition, and a console type with a field for an oracle would be a console that could
 * render one.
 */

import { DecodeError } from "@/lib/insights/decode";

/**
 * The shape of one generated mission, as the backend names it.
 *
 * A string rather than a union, because a workspace built by an older generator may carry a
 * family this build does not know and a decoder that refused one would make an old setup
 * unreadable. `missionFamilyLabel` is where an unknown one becomes readable prose.
 */
export type MissionFamily = string;

export interface EvaluationCatalogSummary {
  readonly products: number;
  readonly variants: number;
  /** What a buyer could actually take away today, which is not how many were listed. */
  readonly purchasable_variants: number;
  /**
   * How many of those lines hold a depth AgentRank supplied, and the depth it supplied.
   *
   * A merchant who published that something is in stock published no count, and an evaluation
   * world holds an exact number of units, so the bootstrap states one. It is a simulation
   * parameter frozen into the setup's identity and it is rendered as one: reporting those lines
   * as ordinary stock would be the console presenting AgentRank's own assumption as the
   * merchant's fact.
   *
   * Both are null on a setup built before a source document could omit a quantity. That is an
   * absence rather than a zero.
   */
  readonly simulated_stock_variants: number | null;
  readonly assumed_stock_units: number | null;
  readonly currencies: readonly string[];
  readonly categories: readonly string[];
}

export interface MissionFamilyCount {
  readonly family: MissionFamily;
  readonly missions: number;
  readonly purchase_available: number;
  readonly no_acceptable_purchase: number;
}

export interface UnsupportedFamily {
  readonly family: MissionFamily;
  readonly reason: string;
}

export interface EvaluationWorkspace {
  readonly workspace_id: string;
  readonly created_at: string;
  readonly source_snapshot_id: string;
  readonly source_snapshot_label: string;
  readonly environment_id: string;
  readonly environment_label: string;
  readonly suite_id: string;
  readonly suite_label: string;
  readonly mission_count: number;
  readonly catalog: EvaluationCatalogSummary;
  readonly composition: readonly MissionFamilyCount[];
  readonly unsupported: readonly UnsupportedFamily[];
  readonly generator_version: string;
  readonly configuration_digest: string;
  readonly catalog_hash: string;
  readonly suite_hash: string;
}

export interface PlannedWorkspace {
  readonly mission_count: number;
  readonly catalog: EvaluationCatalogSummary;
  readonly composition: readonly MissionFamilyCount[];
  readonly unsupported: readonly UnsupportedFamily[];
  /** Source fields the evaluation catalog does not carry, as source field addresses. */
  readonly omitted_fields: readonly string[];
  readonly mission_budget: number;
}

export interface SetupBlocker {
  readonly code: string;
  readonly message: string;
}

export interface EvaluationSetup {
  readonly buildable: boolean;
  readonly current_source_snapshot_id: string | null;
  readonly current_source_snapshot_label: string | null;
  /**
   * Whether the merchant has published evidence their current setup was not built from. A fact
   * rather than a warning: nothing is rebuilt and no earlier run is invalidated by it.
   */
  readonly source_is_newer_than_the_workspace: boolean;
  readonly workspace: EvaluationWorkspace | null;
  /**
   * A benchmark world this merchant has that no setup here generated, when they have one.
   *
   * A merchant an operator registered from authored files is evaluable and simply was not set
   * up by this mechanism, so the console reports that rather than telling a working merchant
   * their setup is blocked.
   */
  readonly operator_world_label: string | null;
  readonly planned: PlannedWorkspace | null;
  readonly blockers: readonly SetupBlocker[];
}

function object(value: unknown, where: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DecodeError(`${where}: expected an object`);
  }
  return value as Record<string, unknown>;
}

function array(value: unknown, where: string): unknown[] {
  if (!Array.isArray(value)) throw new DecodeError(`${where}: expected an array`);
  return value;
}

function string(value: unknown, where: string): string {
  if (typeof value !== "string") throw new DecodeError(`${where}: expected a string`);
  return value;
}

function nullableString(value: unknown, where: string): string | null {
  return value === null ? null : string(value, where);
}

function integer(value: unknown, where: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new DecodeError(`${where}: expected an integer`);
  }
  return value;
}

function nullableInteger(value: unknown, where: string): number | null {
  return value === null ? null : integer(value, where);
}

function bool(value: unknown, where: string): boolean {
  if (typeof value !== "boolean") throw new DecodeError(`${where}: expected a boolean`);
  return value;
}

function strings(value: unknown, where: string): string[] {
  return array(value, where).map((item) => string(item, where));
}

function catalog(value: unknown): EvaluationCatalogSummary {
  const source = object(value, "catalog");
  return {
    products: integer(source.products, "catalog products"),
    variants: integer(source.variants, "catalog variants"),
    purchasable_variants: integer(source.purchasable_variants, "catalog purchasable_variants"),
    simulated_stock_variants: nullableInteger(
      source.simulated_stock_variants,
      "catalog simulated_stock_variants",
    ),
    assumed_stock_units: nullableInteger(source.assumed_stock_units, "catalog assumed_stock_units"),
    currencies: strings(source.currencies, "catalog currencies"),
    categories: strings(source.categories, "catalog categories"),
  };
}

function composition(value: unknown): MissionFamilyCount[] {
  return array(value, "composition").map((item) => {
    const entry = object(item, "mission family");
    return {
      family: string(entry.family, "mission family"),
      missions: integer(entry.missions, "mission family missions"),
      purchase_available: integer(entry.purchase_available, "mission family available"),
      no_acceptable_purchase: integer(
        entry.no_acceptable_purchase,
        "mission family no acceptable purchase",
      ),
    };
  });
}

function unsupported(value: unknown): UnsupportedFamily[] {
  return array(value, "unsupported").map((item) => {
    const entry = object(item, "unsupported family");
    return {
      family: string(entry.family, "unsupported family"),
      reason: string(entry.reason, "unsupported family reason"),
    };
  });
}

export function decodeWorkspace(value: unknown): EvaluationWorkspace {
  const source = object(value, "evaluation workspace");
  return {
    workspace_id: string(source.workspace_id, "workspace_id"),
    created_at: string(source.created_at, "created_at"),
    source_snapshot_id: string(source.source_snapshot_id, "source_snapshot_id"),
    source_snapshot_label: string(source.source_snapshot_label, "source_snapshot_label"),
    environment_id: string(source.environment_id, "environment_id"),
    environment_label: string(source.environment_label, "environment_label"),
    suite_id: string(source.suite_id, "suite_id"),
    suite_label: string(source.suite_label, "suite_label"),
    mission_count: integer(source.mission_count, "mission_count"),
    catalog: catalog(source.catalog),
    composition: composition(source.composition),
    unsupported: unsupported(source.unsupported),
    generator_version: string(source.generator_version, "generator_version"),
    configuration_digest: string(source.configuration_digest, "configuration_digest"),
    catalog_hash: string(source.catalog_hash, "catalog_hash"),
    suite_hash: string(source.suite_hash, "suite_hash"),
  };
}

function planned(value: unknown): PlannedWorkspace {
  const source = object(value, "planned workspace");
  return {
    mission_count: integer(source.mission_count, "planned mission_count"),
    catalog: catalog(source.catalog),
    composition: composition(source.composition),
    unsupported: unsupported(source.unsupported),
    omitted_fields: strings(source.omitted_fields, "omitted_fields"),
    mission_budget: integer(source.mission_budget, "mission_budget"),
  };
}

export function decodeEvaluationSetup(value: unknown): EvaluationSetup {
  const source = object(value, "evaluation setup");
  return {
    buildable: bool(source.buildable, "buildable"),
    current_source_snapshot_id: nullableString(
      source.current_source_snapshot_id,
      "current_source_snapshot_id",
    ),
    current_source_snapshot_label: nullableString(
      source.current_source_snapshot_label,
      "current_source_snapshot_label",
    ),
    source_is_newer_than_the_workspace: bool(
      source.source_is_newer_than_the_workspace,
      "source_is_newer_than_the_workspace",
    ),
    workspace: source.workspace === null ? null : decodeWorkspace(source.workspace),
    operator_world_label: nullableString(source.operator_world_label, "operator_world_label"),
    planned: source.planned === null ? null : planned(source.planned),
    blockers: array(source.blockers, "blockers").map((item) => {
      const blocker = object(item, "blocker");
      return {
        code: string(blocker.code, "blocker code"),
        message: string(blocker.message, "blocker message"),
      };
    }),
  };
}
