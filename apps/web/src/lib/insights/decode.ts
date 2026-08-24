/**
 * Field by field validation of insights API responses into the typed contracts.
 *
 * The same rule `src/lib/razorpay.ts` established: these values decide what a merchant is
 * told, so nothing is cast and everything is checked. A decoder either returns a fully
 * typed value or throws a sentence naming the field that disagreed. Keeping wire names
 * verbatim makes a contract change visible as a diff here rather than as undefined
 * properties downstream.
 */

import type {
  ActionabilityValue,
  ArmAggregate,
  ComparisonConclusion,
  CurrencyDelta,
  DiagnosticOwnerValue,
  EvidenceLevelValue,
  EvidenceReference,
  ExperimentComparison,
  LatestExperiment,
  MerchantFinding,
  MerchantOverview,
  MethodologyWarning,
  MissionDiagnosis,
  MissionFinding,
  MissionTransition,
  RepresentationState,
  RunDiagnostics,
  RunMetrics,
  RunProviderHealth,
  RunSummary,
  SeverityValue,
  SimulatedDemandBucket,
  SimulatedDemandEffect,
  TraceEventItem,
  TraceProjection,
} from "./types";

export class DecodeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DecodeError";
  }
}

interface Entry {
  readonly object: Record<string, unknown>;
}

function entry(value: unknown, where: string): Entry {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DecodeError(`${where}: expected an object`);
  }
  return { object: value as Record<string, unknown> };
}

function string(source: Entry, field: string): string {
  const value = source.object[field];
  if (typeof value !== "string") {
    throw new DecodeError(`${field}: expected a string`);
  }
  return value;
}

function optionalString(source: Entry, field: string): string | null {
  const value = source.object[field];
  if (value === null) {
    return null;
  }
  if (typeof value !== "string") {
    throw new DecodeError(`${field}: expected a string or null`);
  }
  return value;
}

function integer(source: Entry, field: string): number {
  const value = source.object[field];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new DecodeError(`${field}: expected an integer`);
  }
  return value;
}

function optionalInteger(source: Entry, field: string): number | null {
  const value = source.object[field];
  if (value === null) {
    return null;
  }
  return integer(source, field);
}

function optionalRate(source: Entry, field: string): number | null {
  const value = source.object[field];
  if (value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new DecodeError(`${field}: expected a finite number or null`);
  }
  return value;
}

function boolean(source: Entry, field: string): boolean {
  const value = source.object[field];
  if (typeof value !== "boolean") {
    throw new DecodeError(`${field}: expected a boolean`);
  }
  return value;
}

function optionalBoolean(source: Entry, field: string): boolean | null {
  const value = source.object[field];
  if (value === null) {
    return null;
  }
  return boolean(source, field);
}

function enumerated<T extends string>(source: Entry, field: string, allowed: readonly T[]): T {
  const value = string(source, field);
  if (!(allowed as readonly string[]).includes(value)) {
    throw new DecodeError(`${field}: unexpected value ${JSON.stringify(value)}`);
  }
  return value as T;
}

function array(
  source: Entry,
  field: string,
  readItem: (item: unknown, where: string) => unknown,
): unknown[] {
  const value = source.object[field];
  if (!Array.isArray(value)) {
    throw new DecodeError(`${field}: expected an array`);
  }
  return value.map((item, index) => readItem(item, `${field}[${String(index)}]`));
}

const OWNERS: readonly DiagnosticOwnerValue[] = [
  "MERCHANT_CATALOG",
  "MERCHANT_REVIEW",
  "COMPILER",
  "BUYER_AGENT",
  "MODEL_PROVIDER",
  "COMMERCE_RUNTIME",
  "PAYMENT_PROVIDER",
  "BENCHMARK_INFRASTRUCTURE",
  "UNKNOWN",
];

const ACTIONABILITIES: readonly ActionabilityValue[] = [
  "MERCHANT_ACTION",
  "NO_MERCHANT_ACTION",
  "AGENT_SYSTEM_ACTION",
  "REVIEW_REQUIRED",
];

const SEVERITIES: readonly SeverityValue[] = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];

const EVIDENCE_LEVELS: readonly EvidenceLevelValue[] = [
  "TRUSTED_FACT",
  "DETERMINISTIC_ATTRIBUTION",
  "UNRESOLVED",
];

function strings(source: Entry, field: string): string[] {
  return array(source, field, (item, where) => {
    if (typeof item !== "string") {
      throw new DecodeError(`${where}: expected a string`);
    }
    return item;
  }) as string[];
}

function objects(source: Entry, field: string): unknown[] {
  return array(source, field, (item, where) => {
    entry(item, where);
    return item;
  });
}

export function decodeSimulatedDemandEffect(value: unknown): SimulatedDemandEffect {
  const source = entry(value, "simulated demand effect");
  return {
    currency: string(source, "currency"),
    bucket: string(source, "bucket"),
    simulated_amount_minor: integer(source, "simulated_amount_minor"),
  };
}

export function decodeSimulatedDemandBucket(value: unknown): SimulatedDemandBucket {
  const source = entry(value, "simulated demand bucket");
  return {
    currency: string(source, "currency"),
    simulated_potential_demand_amount_minor: integer(
      source,
      "simulated_potential_demand_amount_minor",
    ),
    simulated_captured_demand_amount_minor: integer(
      source,
      "simulated_captured_demand_amount_minor",
    ),
    simulated_lost_demand_amount_minor: integer(source, "simulated_lost_demand_amount_minor"),
    simulated_not_measured_demand_amount_minor: integer(
      source,
      "simulated_not_measured_demand_amount_minor",
    ),
  };
}

function decodeEvidenceReference(value: unknown): EvidenceReference {
  const source = entry(value, "evidence reference");
  return {
    kind: string(source, "kind"),
    identifier: string(source, "identifier"),
    establishes: string(source, "establishes"),
  };
}

function decodeMissionFinding(value: unknown): MissionFinding {
  const source = entry(value, "mission finding");
  return {
    code: string(source, "code"),
    owner: enumerated(source, "owner", OWNERS),
    actionability: enumerated(source, "actionability", ACTIONABILITIES),
    severity: enumerated(source, "severity", SEVERITIES),
    evidence_level: enumerated(source, "evidence_level", EVIDENCE_LEVELS),
    summary: string(source, "summary"),
    recommendation: optionalString(source, "recommendation"),
    attribute_keys: strings(source, "attribute_keys"),
    product_ids: strings(source, "product_ids"),
    variant_ids: strings(source, "variant_ids"),
    evidence: objects(source, "evidence").map(decodeEvidenceReference),
  };
}

export function decodeMissionDiagnosis(value: unknown): MissionDiagnosis {
  const source = entry(value, "mission diagnosis");
  return {
    engine_identity: string(source, "engine_identity"),
    run_id: string(source, "run_id"),
    mission_run_id: string(source, "mission_run_id"),
    mission_key: string(source, "mission_key"),
    status: string(source, "status"),
    outcome: string(source, "outcome"),
    primary_code: optionalString(source, "primary_code"),
    findings: objects(source, "findings").map(decodeMissionFinding),
    simulated_demand: objects(source, "simulated_demand").map(decodeSimulatedDemandEffect),
    model_invocations: optionalInteger(source, "model_invocations"),
    tool_calls: optionalInteger(source, "tool_calls"),
    tool_errors: optionalInteger(source, "tool_errors"),
  };
}

export function decodeRunMetrics(value: unknown): RunMetrics {
  const source = entry(value, "run metrics");
  const counts = source.object["primary_failure_counts"];
  if (typeof counts !== "object" || counts === null || Array.isArray(counts)) {
    throw new DecodeError("primary_failure_counts: expected an object");
  }
  for (const [reason, count] of Object.entries(counts)) {
    if (typeof count !== "number" || !Number.isInteger(count)) {
      throw new DecodeError(`primary_failure_counts.${reason}: expected an integer`);
    }
  }
  return {
    missions_total: integer(source, "missions_total"),
    missions_succeeded: integer(source, "missions_succeeded"),
    missions_failed: integer(source, "missions_failed"),
    missions_abstained: integer(source, "missions_abstained"),
    missions_errored: integer(source, "missions_errored"),
    missions_unfinished: integer(source, "missions_unfinished"),
    purchase_missions: integer(source, "purchase_missions"),
    control_missions: integer(source, "control_missions"),
    correct_abstentions: integer(source, "correct_abstentions"),
    incorrect_abstentions: integer(source, "incorrect_abstentions"),
    task_completion_rate: optionalRate(source, "task_completion_rate"),
    correct_abstention_rate: optionalRate(source, "correct_abstention_rate"),
    unsafe_attempts: integer(source, "unsafe_attempts"),
    unverified_attempts: integer(source, "unverified_attempts"),
    unsafe_completions: integer(source, "unsafe_completions"),
    mandate_denials_protecting: integer(source, "mandate_denials_protecting"),
    mandate_denials_on_compliant_attempt: integer(source, "mandate_denials_on_compliant_attempt"),
    oracle_disagreements: integer(source, "oracle_disagreements"),
    oracle_unchecked: integer(source, "oracle_unchecked"),
    primary_failure_counts: counts as Readonly<Record<string, number>>,
  };
}

export function decodeMerchantFinding(value: unknown): MerchantFinding {
  const source = entry(value, "merchant finding");
  return {
    key: string(source, "key"),
    code: string(source, "code"),
    owner: enumerated(source, "owner", OWNERS),
    actionability: enumerated(source, "actionability", ACTIONABILITIES),
    severity: enumerated(source, "severity", SEVERITIES),
    evidence_level: enumerated(source, "evidence_level", EVIDENCE_LEVELS),
    title: string(source, "title"),
    recommendation: optionalString(source, "recommendation"),
    mission_run_ids: strings(source, "mission_run_ids"),
    mission_keys: strings(source, "mission_keys"),
    product_ids: strings(source, "product_ids"),
    variant_ids: strings(source, "variant_ids"),
    attribute_keys: strings(source, "attribute_keys"),
    simulated_demand: objects(source, "simulated_demand").map(decodeSimulatedDemandEffect),
  };
}

function decodeRunProviderHealth(value: unknown): RunProviderHealth {
  const source = entry(value, "provider health");
  return {
    missions_with_provider_errors: integer(source, "missions_with_provider_errors"),
    terminated_outages: integer(source, "terminated_outages"),
    recovered_throttles: integer(source, "recovered_throttles"),
    requested_model: optionalString(source, "requested_model"),
    resolved_models: strings(source, "resolved_models"),
  };
}

export function decodeRunDiagnostics(value: unknown): RunDiagnostics {
  const source = entry(value, "run diagnostics");
  return {
    engine_identity: string(source, "engine_identity"),
    run_id: string(source, "run_id"),
    status: string(source, "status"),
    suite_label: string(source, "suite_label"),
    environment_label: optionalString(source, "environment_label"),
    representation_id: optionalString(source, "representation_id"),
    representation_label: optionalString(source, "representation_label"),
    catalog_hash: optionalString(source, "catalog_hash"),
    evaluator_version: optionalString(source, "evaluator_version"),
    executor_label: optionalString(source, "executor_label"),
    executor_revision: optionalString(source, "executor_revision"),
    agent_implementation_version: optionalInteger(source, "agent_implementation_version"),
    benchmark_designation: optionalString(source, "benchmark_designation"),
    created_at: string(source, "created_at"),
    started_at: optionalString(source, "started_at"),
    completed_at: optionalString(source, "completed_at"),
    catalog_pin_verified: optionalBoolean(source, "catalog_pin_verified"),
    metrics: decodeRunMetrics(entry(source.object["metrics"], "metrics").object),
    findings: objects(source, "findings").map(decodeMerchantFinding),
    missions: objects(source, "missions").map(decodeMissionDiagnosis),
    provider_health: decodeRunProviderHealth(
      entry(source.object["provider_health"], "provider_health").object,
    ),
    simulated_demand: objects(source, "simulated_demand").map(decodeSimulatedDemandBucket),
  };
}

export function decodeTraceProjection(value: unknown): TraceProjection {
  const source = entry(value, "trace projection");
  const events = objects(source, "events").map((rawEvent) => {
    const eventSource = entry(rawEvent, "events[]");
    const sequence = eventSource.object["sequence"];
    const payload = eventSource.object["payload"];
    if (
      typeof sequence !== "number" ||
      !Number.isInteger(sequence) ||
      typeof payload !== "object" ||
      payload === null ||
      Array.isArray(payload)
    ) {
      throw new DecodeError("events[]: expected an integer sequence and an object payload");
    }
    const recordedAt = eventSource.object["recorded_at"];
    if (typeof recordedAt !== "string") {
      throw new DecodeError("events[].recorded_at: expected a string");
    }
    const eventType = eventSource.object["event_type"];
    if (typeof eventType !== "string") {
      throw new DecodeError("events[].event_type: expected a string");
    }
    const item: TraceEventItem = {
      sequence,
      event_type: eventType,
      recorded_at: recordedAt,
      payload: payload as Readonly<Record<string, unknown>>,
    };
    return item;
  });
  return {
    total_events: integer(source, "total_events"),
    events,
  };
}

export function decodeRunSummary(value: unknown): RunSummary {
  const source = entry(value, "run summary");
  return {
    run_id: string(source, "run_id"),
    status: string(source, "status"),
    suite_label: string(source, "suite_label"),
    executor_label: optionalString(source, "executor_label"),
    benchmark_designation: optionalString(source, "benchmark_designation"),
    started_at: optionalString(source, "started_at"),
    completed_at: optionalString(source, "completed_at"),
    missions_total: integer(source, "missions_total"),
    missions_succeeded: integer(source, "missions_succeeded"),
    missions_failed: integer(source, "missions_failed"),
    missions_abstained: integer(source, "missions_abstained"),
    missions_errored: integer(source, "missions_errored"),
    task_completion_rate: optionalRate(source, "task_completion_rate"),
    correct_abstention_rate: optionalRate(source, "correct_abstention_rate"),
    unsafe_attempts: integer(source, "unsafe_attempts"),
    unsafe_completions: integer(source, "unsafe_completions"),
    provider_failure_missions: integer(source, "provider_failure_missions"),
    simulated_demand: objects(source, "simulated_demand").map(decodeSimulatedDemandBucket),
  };
}

export function decodeRunSummaryList(value: unknown): RunSummary[] {
  if (!Array.isArray(value)) {
    throw new DecodeError("run summaries: expected an array");
  }
  return value.map((item) => decodeRunSummary(item));
}

function decodeRepresentationState(value: unknown): RepresentationState {
  const source = entry(value, "representation state");
  return {
    source_snapshot_id: optionalString(source, "source_snapshot_id"),
    source_snapshot_label: optionalString(source, "source_snapshot_label"),
    compiled_representation_id: optionalString(source, "compiled_representation_id"),
    compiled_representation_label: optionalString(source, "compiled_representation_label"),
    review_required_facts: integer(source, "review_required_facts"),
  };
}

function decodeMethodologyWarning(value: unknown): MethodologyWarning {
  const source = entry(value, "methodology warning");
  return {
    code: string(source, "code"),
    message: string(source, "message"),
  };
}

function decodeLatestExperiment(value: unknown): LatestExperiment | null {
  if (value === null) {
    return null;
  }
  const source = entry(value, "latest experiment");
  return {
    experiment_id: string(source, "experiment_id"),
    benchmark_designation: string(source, "benchmark_designation"),
    completed_sample_pairs: integer(source, "completed_sample_pairs"),
    conclusion_kind: string(source, "conclusion_kind"),
    conclusion_statement: string(source, "conclusion_statement"),
    warnings: objects(source, "warnings").map(decodeMethodologyWarning),
  };
}

export function decodeMerchantOverview(value: unknown): MerchantOverview {
  const source = entry(value, "merchant overview");
  return {
    engine_identity: string(source, "engine_identity"),
    merchant_id: string(source, "merchant_id"),
    runs: objects(source, "runs").map(decodeRunSummary),
    top_findings: objects(source, "top_findings").map(decodeMerchantFinding),
    top_findings_run_id: optionalString(source, "top_findings_run_id"),
    simulated_demand_totals_by_currency: objects(source, "simulated_demand_totals_by_currency").map(
      decodeSimulatedDemandBucket,
    ),
    latest_experiment: decodeLatestExperiment(source.object["latest_experiment"]),
    representation_state: decodeRepresentationState(
      entry(source.object["representation_state"], "representation_state").object,
    ),
  };
}

function decodeArmAggregate(value: unknown): ArmAggregate {
  const source = entry(value, "experiment arm");
  const totals = source.object["metrics_totals"];
  return {
    arm: string(source, "arm"),
    planned_samples: integer(source, "planned_samples"),
    completed_samples: integer(source, "completed_samples"),
    completion_rate_mean: optionalRate(source, "completion_rate_mean"),
    terminated_provider_outages: integer(source, "terminated_provider_outages"),
    missions_with_provider_errors: integer(source, "missions_with_provider_errors"),
    model_invocations: integer(source, "model_invocations"),
    tool_calls: integer(source, "tool_calls"),
    resolved_models: strings(source, "resolved_models"),
    metrics_totals:
      totals === null ? null : decodeRunMetrics(entry(totals, "metrics_totals").object),
  };
}

function decodeCurrencyDelta(value: unknown): CurrencyDelta {
  const source = entry(value, "demand delta");
  return {
    currency: string(source, "currency"),
    simulated_potential_delta_amount_minor: integer(
      source,
      "simulated_potential_delta_amount_minor",
    ),
    simulated_captured_delta_amount_minor: integer(source, "simulated_captured_delta_amount_minor"),
    simulated_lost_delta_amount_minor: integer(source, "simulated_lost_delta_amount_minor"),
    simulated_not_measured_delta_amount_minor: integer(
      source,
      "simulated_not_measured_delta_amount_minor",
    ),
  };
}

function decodeMissionTransition(value: unknown): MissionTransition {
  const source = entry(value, "mission transition");
  return {
    pair_ordinal: integer(source, "pair_ordinal"),
    mission_key: string(source, "mission_key"),
    raw_status: string(source, "raw_status"),
    raw_primary_failure_reason: optionalString(source, "raw_primary_failure_reason"),
    compiled_status: string(source, "compiled_status"),
    compiled_primary_failure_reason: optionalString(source, "compiled_primary_failure_reason"),
    direction: string(source, "direction"),
  };
}

export function decodeExperimentComparison(value: unknown): ExperimentComparison {
  const source = entry(value, "experiment comparison");
  const conclusion = entry(source.object["conclusion"], "conclusion");
  const decodedConclusion: ComparisonConclusion = {
    kind: string(conclusion, "kind"),
    statement: string(conclusion, "statement"),
  };
  return {
    engine_identity: string(source, "engine_identity"),
    experiment_id: string(source, "experiment_id"),
    buyer_configuration_digest: string(source, "buyer_configuration_digest"),
    benchmark_designation: string(source, "benchmark_designation"),
    pair_order: string(source, "pair_order"),
    declared_sample_pairs: integer(source, "declared_sample_pairs"),
    completed_sample_pairs: integer(source, "completed_sample_pairs"),
    arms: objects(source, "arms").map(decodeArmAggregate),
    demand_delta_by_currency: objects(source, "demand_delta_by_currency").map(decodeCurrencyDelta),
    mission_transitions: objects(source, "mission_transitions").map(decodeMissionTransition),
    warnings: objects(source, "warnings").map(decodeMethodologyWarning),
    conclusion: decodedConclusion,
  };
}
