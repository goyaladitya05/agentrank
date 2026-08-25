/**
 * The shapes the evaluation API returns, restated for the console and validated by hand.
 *
 * The same rule the insights decoders follow: these values decide what a merchant is told
 * before they spend a benchmark run, so nothing is cast and every field is checked by name.
 * Wire names are kept verbatim, which makes a backend contract change a diff here rather than
 * an undefined property in a panel.
 */

import { DecodeError } from "@/lib/insights/decode";

export type LaunchStatus = "QUEUED" | "EXECUTING" | "COMPLETED" | "FAILED";
export type BuyerProfile = "AI_BUYER" | "REFERENCE_BUYER";
/**
 * Which command the server decided this merchant is making.
 *
 * INITIAL measures the merchant's current merchant-facing state and has no before.
 * REEVALUATION measures a published agent-ready representation against a prior run.
 */
export type EvaluationPurpose = "INITIAL" | "REEVALUATION";

export interface LaunchBlocker {
  readonly code: string;
  readonly message: string;
}

export interface EvaluationPreflight {
  readonly launchable: boolean;
  readonly purpose: EvaluationPurpose;
  /** Covers every identity field below. The launch carries it back so a plan that moved since
   * this page rendered is refused rather than frozen silently. */
  readonly plan_digest: string;
  readonly representation_id: string | null;
  readonly representation_label: string | null;
  readonly compiler_run_id: string | null;
  readonly source_snapshot_id: string | null;
  /** Named only when the source is what is being measured. See the backend plan. */
  readonly source_snapshot_label: string | null;
  readonly suite_id: string | null;
  readonly suite_label: string | null;
  readonly suite_definition_hash: string | null;
  readonly mission_count: number | null;
  readonly environment_id: string | null;
  readonly environment_label: string | null;
  readonly buyer_profile: BuyerProfile;
  readonly executor_kind: string;
  readonly provider: string | null;
  readonly requested_model: string | null;
  readonly max_model_turns: number | null;
  readonly max_tool_calls: number | null;
  readonly mission_deadline_seconds: number | null;
  readonly baseline_run_id: string | null;
  readonly baseline_run_completed_at: string | null;
  readonly pending_launch_id: string | null;
  readonly blockers: readonly LaunchBlocker[];
}

export interface EvaluationLaunch {
  readonly launch_id: string;
  readonly purpose: EvaluationPurpose;
  readonly status: LaunchStatus;
  readonly failure_code: string | null;
  readonly requested_at: string;
  readonly started_at: string | null;
  readonly settled_at: string | null;
  readonly representation_id: string | null;
  readonly representation_label: string | null;
  readonly compiler_run_id: string | null;
  readonly source_snapshot_id: string | null;
  readonly source_snapshot_label: string | null;
  readonly suite_id: string;
  readonly suite_label: string;
  readonly mission_count: number;
  readonly environment_label: string;
  readonly buyer_profile: BuyerProfile;
  readonly executor_kind: string;
  readonly provider: string | null;
  readonly requested_model: string | null;
  readonly buyer_configuration_digest: string | null;
  readonly run_id: string | null;
  readonly run_status: string | null;
  readonly missions_completed: number | null;
  readonly baseline_run_id: string | null;
}

export interface CountChange {
  readonly key: string;
  readonly before: number;
  readonly after: number;
  readonly delta: number;
}

export interface RateChange {
  readonly key: string;
  readonly before: number | null;
  readonly after: number | null;
  readonly delta: number | null;
}

export interface SimulatedDemandChange {
  readonly currency: string;
  readonly bucket: string;
  readonly simulated_before_amount_minor: number;
  readonly simulated_after_amount_minor: number;
  readonly simulated_delta_amount_minor: number;
}

export interface ComparisonMissionTransition {
  readonly mission_key: string;
  readonly before_status: string | null;
  readonly before_primary_failure_reason: string | null;
  readonly after_status: string | null;
  readonly after_primary_failure_reason: string | null;
  readonly direction: string;
}

export interface InteractionChange {
  readonly model_invocations: CountChange | null;
  readonly tool_calls: CountChange | null;
  readonly baseline_traced: boolean;
  readonly candidate_traced: boolean;
  /** True, false, or null when at least one run recorded no provider invocation at all. */
  readonly token_usage_complete: boolean | null;
}

export interface RunComparison {
  readonly engine_identity: string;
  readonly baseline_run_id: string;
  readonly candidate_run_id: string;
  readonly comparable: boolean;
  readonly counts: readonly CountChange[];
  readonly rates: readonly RateChange[];
  readonly simulated_demand: readonly SimulatedDemandChange[];
  readonly transitions: readonly ComparisonMissionTransition[];
  readonly interactions: InteractionChange;
  readonly baseline_runtime_seconds: number | null;
  readonly candidate_runtime_seconds: number | null;
  readonly warnings: readonly { readonly code: string; readonly message: string }[];
  readonly conclusion: { readonly kind: string; readonly statement: string };
}

export interface EvaluationLaunchDetail extends EvaluationLaunch {
  readonly comparison: RunComparison | null;
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

function nullableNumber(value: unknown, where: string): number | null {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new DecodeError(`${where}: expected a finite number or null`);
  }
  return value;
}

function bool(value: unknown, where: string): boolean {
  if (typeof value !== "boolean") throw new DecodeError(`${where}: expected a boolean`);
  return value;
}

function nullableBoolean(value: unknown, where: string): boolean | null {
  return value === null ? null : bool(value, where);
}

function status(value: unknown): LaunchStatus {
  const decoded = string(value, "status");
  if (
    decoded !== "QUEUED" &&
    decoded !== "EXECUTING" &&
    decoded !== "COMPLETED" &&
    decoded !== "FAILED"
  ) {
    throw new DecodeError("status: unexpected value");
  }
  return decoded;
}

function purpose(value: unknown): EvaluationPurpose {
  const decoded = string(value, "purpose");
  if (decoded !== "INITIAL" && decoded !== "REEVALUATION") {
    throw new DecodeError("purpose: unexpected value");
  }
  return decoded;
}

function profile(value: unknown): BuyerProfile {
  const decoded = string(value, "buyer_profile");
  if (decoded !== "AI_BUYER" && decoded !== "REFERENCE_BUYER") {
    throw new DecodeError("buyer_profile: unexpected value");
  }
  return decoded;
}

export function decodePreflight(value: unknown): EvaluationPreflight {
  const source = object(value, "re-evaluation preflight");
  return {
    launchable: bool(source.launchable, "launchable"),
    purpose: purpose(source.purpose),
    plan_digest: string(source.plan_digest, "plan_digest"),
    representation_id: nullableString(source.representation_id, "representation_id"),
    representation_label: nullableString(source.representation_label, "representation_label"),
    compiler_run_id: nullableString(source.compiler_run_id, "compiler_run_id"),
    source_snapshot_id: nullableString(source.source_snapshot_id, "source_snapshot_id"),
    source_snapshot_label: nullableString(source.source_snapshot_label, "source_snapshot_label"),
    suite_id: nullableString(source.suite_id, "suite_id"),
    suite_label: nullableString(source.suite_label, "suite_label"),
    suite_definition_hash: nullableString(source.suite_definition_hash, "suite_definition_hash"),
    mission_count: nullableInteger(source.mission_count, "mission_count"),
    environment_id: nullableString(source.environment_id, "environment_id"),
    environment_label: nullableString(source.environment_label, "environment_label"),
    buyer_profile: profile(source.buyer_profile),
    executor_kind: string(source.executor_kind, "executor_kind"),
    provider: nullableString(source.provider, "provider"),
    requested_model: nullableString(source.requested_model, "requested_model"),
    max_model_turns: nullableInteger(source.max_model_turns, "max_model_turns"),
    max_tool_calls: nullableInteger(source.max_tool_calls, "max_tool_calls"),
    mission_deadline_seconds: nullableNumber(
      source.mission_deadline_seconds,
      "mission_deadline_seconds",
    ),
    baseline_run_id: nullableString(source.baseline_run_id, "baseline_run_id"),
    baseline_run_completed_at: nullableString(
      source.baseline_run_completed_at,
      "baseline_run_completed_at",
    ),
    pending_launch_id: nullableString(source.pending_launch_id, "pending_launch_id"),
    blockers: array(source.blockers, "blockers").map((item) => {
      const blocker = object(item, "blocker");
      return {
        code: string(blocker.code, "blocker code"),
        message: string(blocker.message, "blocker message"),
      };
    }),
  };
}

function launchFields(source: Record<string, unknown>): EvaluationLaunch {
  return {
    launch_id: string(source.launch_id, "launch_id"),
    purpose: purpose(source.purpose),
    status: status(source.status),
    failure_code: nullableString(source.failure_code, "failure_code"),
    requested_at: string(source.requested_at, "requested_at"),
    started_at: nullableString(source.started_at, "started_at"),
    settled_at: nullableString(source.settled_at, "settled_at"),
    representation_id: nullableString(source.representation_id, "representation_id"),
    representation_label: nullableString(source.representation_label, "representation_label"),
    compiler_run_id: nullableString(source.compiler_run_id, "compiler_run_id"),
    source_snapshot_id: nullableString(source.source_snapshot_id, "source_snapshot_id"),
    source_snapshot_label: nullableString(source.source_snapshot_label, "source_snapshot_label"),
    suite_id: string(source.suite_id, "suite_id"),
    suite_label: string(source.suite_label, "suite_label"),
    mission_count: integer(source.mission_count, "mission_count"),
    environment_label: string(source.environment_label, "environment_label"),
    buyer_profile: profile(source.buyer_profile),
    executor_kind: string(source.executor_kind, "executor_kind"),
    provider: nullableString(source.provider, "provider"),
    requested_model: nullableString(source.requested_model, "requested_model"),
    buyer_configuration_digest: nullableString(
      source.buyer_configuration_digest,
      "buyer_configuration_digest",
    ),
    run_id: nullableString(source.run_id, "run_id"),
    run_status: nullableString(source.run_status, "run_status"),
    missions_completed: nullableInteger(source.missions_completed, "missions_completed"),
    baseline_run_id: nullableString(source.baseline_run_id, "baseline_run_id"),
  };
}

export function decodeEvaluationLaunch(value: unknown): EvaluationLaunch {
  return launchFields(object(value, "re-evaluation"));
}

export function decodeEvaluationLaunchList(value: unknown): readonly EvaluationLaunch[] {
  return array(value, "re-evaluations").map(decodeEvaluationLaunch);
}

function countChange(value: unknown): CountChange {
  const source = object(value, "count change");
  return {
    key: string(source.key, "key"),
    before: integer(source.before, "before"),
    after: integer(source.after, "after"),
    delta: integer(source.delta, "delta"),
  };
}

function decodeComparison(value: unknown): RunComparison {
  const source = object(value, "run comparison");
  const interactions = object(source.interactions, "interactions");
  const conclusion = object(source.conclusion, "conclusion");
  return {
    engine_identity: string(source.engine_identity, "engine_identity"),
    baseline_run_id: string(source.baseline_run_id, "baseline_run_id"),
    candidate_run_id: string(source.candidate_run_id, "candidate_run_id"),
    comparable: bool(source.comparable, "comparable"),
    counts: array(source.counts, "counts").map(countChange),
    rates: array(source.rates, "rates").map((item) => {
      const rate = object(item, "rate change");
      return {
        key: string(rate.key, "key"),
        before: nullableNumber(rate.before, "before"),
        after: nullableNumber(rate.after, "after"),
        delta: nullableNumber(rate.delta, "delta"),
      };
    }),
    simulated_demand: array(source.simulated_demand, "simulated demand").map((item) => {
      const change = object(item, "simulated demand change");
      return {
        currency: string(change.currency, "currency"),
        bucket: string(change.bucket, "bucket"),
        simulated_before_amount_minor: integer(
          change.simulated_before_amount_minor,
          "simulated_before_amount_minor",
        ),
        simulated_after_amount_minor: integer(
          change.simulated_after_amount_minor,
          "simulated_after_amount_minor",
        ),
        simulated_delta_amount_minor: integer(
          change.simulated_delta_amount_minor,
          "simulated_delta_amount_minor",
        ),
      };
    }),
    transitions: array(source.transitions, "transitions").map((item) => {
      const transition = object(item, "mission transition");
      return {
        mission_key: string(transition.mission_key, "mission_key"),
        before_status: nullableString(transition.before_status, "before_status"),
        before_primary_failure_reason: nullableString(
          transition.before_primary_failure_reason,
          "before_primary_failure_reason",
        ),
        after_status: nullableString(transition.after_status, "after_status"),
        after_primary_failure_reason: nullableString(
          transition.after_primary_failure_reason,
          "after_primary_failure_reason",
        ),
        direction: string(transition.direction, "direction"),
      };
    }),
    interactions: {
      model_invocations:
        interactions.model_invocations === null
          ? null
          : countChange(interactions.model_invocations),
      tool_calls: interactions.tool_calls === null ? null : countChange(interactions.tool_calls),
      baseline_traced: bool(interactions.baseline_traced, "baseline_traced"),
      candidate_traced: bool(interactions.candidate_traced, "candidate_traced"),
      token_usage_complete: nullableBoolean(
        interactions.token_usage_complete,
        "token_usage_complete",
      ),
    },
    baseline_runtime_seconds: nullableNumber(
      source.baseline_runtime_seconds,
      "baseline_runtime_seconds",
    ),
    candidate_runtime_seconds: nullableNumber(
      source.candidate_runtime_seconds,
      "candidate_runtime_seconds",
    ),
    warnings: array(source.warnings, "warnings").map((item) => {
      const warning = object(item, "methodology warning");
      return {
        code: string(warning.code, "code"),
        message: string(warning.message, "message"),
      };
    }),
    conclusion: {
      kind: string(conclusion.kind, "kind"),
      statement: string(conclusion.statement, "statement"),
    },
  };
}

export function decodeEvaluationLaunchDetail(value: unknown): EvaluationLaunchDetail {
  const source = object(value, "re-evaluation detail");
  return {
    ...launchFields(source),
    comparison: source.comparison === null ? null : decodeComparison(source.comparison),
  };
}
