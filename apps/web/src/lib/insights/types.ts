/**
 * The shapes the AgentRank insights API returns, restated for the console.
 *
 * Every interface here mirrors one Pydantic view in
 * `agentrank_api.diagnostics.schemas` field for field, with the wire's snake_case kept
 * verbatim. A decoder in `decode.ts` is the only code allowed to produce these objects,
 * and it validates every field by name, so a backend contract change surfaces as a loud
 * decode failure rather than as a silently undefined property somewhere in the UI.
 */

export type DiagnosticOwnerValue =
  | "MERCHANT_CATALOG"
  | "MERCHANT_REVIEW"
  | "COMPILER"
  | "BUYER_AGENT"
  | "MODEL_PROVIDER"
  | "COMMERCE_RUNTIME"
  | "PAYMENT_PROVIDER"
  | "BENCHMARK_INFRASTRUCTURE"
  | "UNKNOWN";

export type ActionabilityValue =
  "MERCHANT_ACTION" | "NO_MERCHANT_ACTION" | "AGENT_SYSTEM_ACTION" | "REVIEW_REQUIRED";

export type SeverityValue = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";

export type EvidenceLevelValue = "TRUSTED_FACT" | "DETERMINISTIC_ATTRIBUTION" | "UNRESOLVED";

export interface SimulatedDemandEffect {
  readonly currency: string;
  readonly bucket: string;
  readonly simulated_amount_minor: number;
}

export interface SimulatedDemandBucket {
  readonly currency: string;
  readonly simulated_potential_demand_amount_minor: number;
  readonly simulated_captured_demand_amount_minor: number;
  readonly simulated_lost_demand_amount_minor: number;
  readonly simulated_not_measured_demand_amount_minor: number;
}

export interface EvidenceReference {
  readonly kind: string;
  readonly identifier: string;
  readonly establishes: string;
}

/**
 * One compiler candidate a merchant can act on a finding through.
 *
 * Present only where the API could prove the relationship. An empty list means the compiler
 * run behind the tested representation proposes nothing at this finding's address, or that the
 * run under diagnosis was not measured against a compiler-produced representation at all.
 */
export interface CompilerReference {
  readonly compiler_run_id: string;
  readonly candidate_id: string;
  readonly target: string;
}

export interface MissionFinding {
  readonly code: string;
  readonly owner: DiagnosticOwnerValue;
  readonly actionability: ActionabilityValue;
  readonly severity: SeverityValue;
  readonly evidence_level: EvidenceLevelValue;
  readonly summary: string;
  readonly recommendation: string | null;
  readonly attribute_keys: readonly string[];
  readonly product_ids: readonly string[];
  readonly variant_ids: readonly string[];
  readonly evidence: readonly EvidenceReference[];
  readonly compiler_references: readonly CompilerReference[];
}

export interface MissionDiagnosis {
  readonly engine_identity: string;
  readonly run_id: string;
  readonly mission_run_id: string;
  readonly mission_key: string;
  readonly status: string;
  readonly outcome: string;
  readonly primary_code: string | null;
  readonly findings: readonly MissionFinding[];
  readonly simulated_demand: readonly SimulatedDemandEffect[];
  readonly model_invocations: number | null;
  readonly tool_calls: number | null;
  readonly tool_errors: number | null;
}

export interface RunMetrics {
  readonly missions_total: number;
  readonly missions_succeeded: number;
  readonly missions_failed: number;
  readonly missions_abstained: number;
  readonly missions_errored: number;
  readonly missions_unfinished: number;
  readonly purchase_missions: number;
  readonly control_missions: number;
  readonly correct_abstentions: number;
  readonly incorrect_abstentions: number;
  readonly task_completion_rate: number | null;
  readonly correct_abstention_rate: number | null;
  readonly unsafe_attempts: number;
  readonly unverified_attempts: number;
  readonly unsafe_completions: number;
  readonly mandate_denials_protecting: number;
  readonly mandate_denials_on_compliant_attempt: number;
  readonly oracle_disagreements: number;
  readonly oracle_unchecked: number;
  readonly primary_failure_counts: Readonly<Record<string, number>>;
}

export interface MerchantFinding {
  readonly key: string;
  readonly code: string;
  readonly owner: DiagnosticOwnerValue;
  readonly actionability: ActionabilityValue;
  readonly severity: SeverityValue;
  readonly evidence_level: EvidenceLevelValue;
  readonly title: string;
  readonly recommendation: string | null;
  readonly mission_run_ids: readonly string[];
  readonly mission_keys: readonly string[];
  readonly product_ids: readonly string[];
  readonly variant_ids: readonly string[];
  readonly attribute_keys: readonly string[];
  readonly simulated_demand: readonly SimulatedDemandEffect[];
  readonly compiler_references: readonly CompilerReference[];
}

export interface RunProviderHealth {
  readonly missions_with_provider_errors: number;
  readonly terminated_outages: number;
  readonly recovered_throttles: number;
  readonly requested_model: string | null;
  readonly resolved_models: readonly string[];
}

export interface RunDiagnostics {
  readonly engine_identity: string;
  readonly run_id: string;
  readonly status: string;
  readonly suite_label: string;
  readonly environment_label: string | null;
  readonly representation_id: string | null;
  readonly representation_label: string | null;
  readonly compiler_run_id: string | null;
  readonly catalog_hash: string | null;
  readonly evaluator_version: string | null;
  readonly executor_label: string | null;
  readonly executor_revision: string | null;
  readonly agent_implementation_version: number | null;
  readonly benchmark_designation: string | null;
  readonly created_at: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly catalog_pin_verified: boolean | null;
  readonly metrics: RunMetrics;
  readonly findings: readonly MerchantFinding[];
  readonly missions: readonly MissionDiagnosis[];
  readonly provider_health: RunProviderHealth;
  readonly simulated_demand: readonly SimulatedDemandBucket[];
}

export interface TraceEventItem {
  readonly sequence: number;
  readonly event_type: string;
  readonly recorded_at: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

export interface TraceProjection {
  readonly total_events: number;
  readonly events: readonly TraceEventItem[];
}

export interface RunSummary {
  readonly run_id: string;
  readonly status: string;
  readonly suite_label: string;
  readonly executor_label: string | null;
  readonly benchmark_designation: string | null;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly missions_total: number;
  readonly missions_succeeded: number;
  readonly missions_failed: number;
  readonly missions_abstained: number;
  readonly missions_errored: number;
  readonly task_completion_rate: number | null;
  readonly correct_abstention_rate: number | null;
  readonly unsafe_attempts: number;
  readonly unsafe_completions: number;
  readonly provider_failure_missions: number;
  readonly simulated_demand: readonly SimulatedDemandBucket[];
}

export interface RepresentationState {
  readonly source_snapshot_id: string | null;
  readonly source_snapshot_label: string | null;
  readonly compiled_representation_id: string | null;
  readonly compiled_representation_label: string | null;
  readonly review_required_facts: number;
}

export interface MethodologyWarning {
  readonly code: string;
  readonly message: string;
}

export interface LatestExperiment {
  readonly experiment_id: string;
  readonly benchmark_designation: string;
  readonly completed_sample_pairs: number;
  readonly conclusion_kind: string;
  readonly conclusion_statement: string;
  readonly warnings: readonly MethodologyWarning[];
}

export interface MerchantOverview {
  readonly engine_identity: string;
  readonly merchant_id: string;
  readonly runs: readonly RunSummary[];
  readonly top_findings: readonly MerchantFinding[];
  readonly top_findings_run_id: string | null;
  readonly simulated_demand_totals_by_currency: readonly SimulatedDemandBucket[];
  readonly latest_experiment: LatestExperiment | null;
  readonly representation_state: RepresentationState;
}

export interface ArmAggregate {
  readonly arm: string;
  readonly planned_samples: number;
  readonly completed_samples: number;
  readonly completion_rate_mean: number | null;
  readonly terminated_provider_outages: number;
  readonly missions_with_provider_errors: number;
  readonly model_invocations: number;
  readonly tool_calls: number;
  readonly resolved_models: readonly string[];
  readonly metrics_totals: RunMetrics | null;
}

export interface CurrencyDelta {
  readonly currency: string;
  readonly simulated_potential_delta_amount_minor: number;
  readonly simulated_captured_delta_amount_minor: number;
  readonly simulated_lost_delta_amount_minor: number;
  readonly simulated_not_measured_delta_amount_minor: number;
}

export interface MissionTransition {
  readonly pair_ordinal: number;
  readonly mission_key: string;
  readonly raw_status: string;
  readonly raw_primary_failure_reason: string | null;
  readonly compiled_status: string;
  readonly compiled_primary_failure_reason: string | null;
  readonly direction: string;
}

export interface ComparisonConclusion {
  readonly kind: string;
  readonly statement: string;
}

export interface ExperimentComparison {
  readonly engine_identity: string;
  readonly experiment_id: string;
  readonly buyer_configuration_digest: string;
  readonly benchmark_designation: string;
  readonly pair_order: string;
  readonly declared_sample_pairs: number;
  readonly completed_sample_pairs: number;
  readonly arms: readonly ArmAggregate[];
  readonly demand_delta_by_currency: readonly CurrencyDelta[];
  readonly mission_transitions: readonly MissionTransition[];
  readonly warnings: readonly MethodologyWarning[];
  readonly conclusion: ComparisonConclusion;
}
