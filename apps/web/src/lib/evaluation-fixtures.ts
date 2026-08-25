/**
 * Fixtures shaped exactly like the evaluation API's JSON responses.
 *
 * They exist so decoder and component tests exercise the wire shapes the console will actually
 * receive rather than approximations of them. Nothing here is imported by a route or a shipped
 * component, and none of it is fake production data.
 */

export const PREFLIGHT_FIXTURE = {
  launchable: true,
  purpose: "REEVALUATION",
  plan_digest: "sha256:" + "9".repeat(64),
  representation_id: "01a00000-0000-7000-8000-000000000002",
  representation_label: "compiler:sha256:cfg000:sha256:ir0000",
  compiler_run_id: "01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1",
  source_snapshot_id: "01a00000-0000-7000-8000-000000000001",
  source_snapshot_label: null,
  suite_id: "01a00000-0000-7000-8000-000000000003",
  suite_label: "voltedge-core@2",
  suite_definition_hash: "sha256:abc123",
  mission_count: 14,
  environment_id: "01a00000-0000-7000-8000-000000000004",
  environment_label: "voltedge-catalog@1 (fixture sha256:def456)",
  buyer_profile: "AI_BUYER",
  executor_kind: "llm-openai",
  provider: "openai-responses",
  requested_model: "gpt-5.6-terra",
  max_model_turns: 12,
  max_tool_calls: 24,
  mission_deadline_seconds: 120.0,
  max_provider_requests: 252,
  max_requests_per_mission: 24,
  execution_budget_version: 1,
  baseline_run_id: "01992222-2222-7222-8222-222222222222",
  baseline_run_completed_at: "2026-08-23T10:04:30Z",
  baseline_surface_matches: true,
  pending_launch_id: null,
  blockers: [],
};

export const REFERENCE_PREFLIGHT_FIXTURE = {
  ...PREFLIGHT_FIXTURE,
  buyer_profile: "REFERENCE_BUYER",
  executor_kind: "reference-isolated",
  provider: null,
  requested_model: null,
  max_model_turns: null,
  max_tool_calls: null,
  mission_deadline_seconds: null,
  max_provider_requests: null,
  max_requests_per_mission: null,
  execution_budget_version: null,
  baseline_run_id: null,
  baseline_run_completed_at: null,
  baseline_surface_matches: null,
};

/** A merchant AgentRank has never measured and who has published nothing. */
export const INITIAL_PREFLIGHT_FIXTURE = {
  ...PREFLIGHT_FIXTURE,
  purpose: "INITIAL",
  representation_id: null,
  representation_label: null,
  compiler_run_id: null,
  source_snapshot_label: "voltedge-source@1",
  baseline_run_id: null,
  baseline_run_completed_at: null,
  baseline_surface_matches: null,
};

/** The same merchant before they have told AgentRank anything about themselves. */
export const INITIAL_BLOCKED_PREFLIGHT_FIXTURE = {
  ...INITIAL_PREFLIGHT_FIXTURE,
  launchable: false,
  source_snapshot_id: null,
  source_snapshot_label: null,
  blockers: [
    {
      code: "merchant_source_unavailable",
      message:
        "AgentRank has no record of your merchant information yet, so there is nothing to evaluate you against. Add your merchant source first.",
    },
  ],
};

/** A merchant whose only earlier run measured the storefront, about to measure an artifact. */
export const SURFACE_CHANGE_PREFLIGHT_FIXTURE = {
  ...PREFLIGHT_FIXTURE,
  baseline_surface_matches: false,
};

export const BLOCKED_PREFLIGHT_FIXTURE = {
  ...PREFLIGHT_FIXTURE,
  launchable: false,
  pending_launch_id: "01a0cccc-cccc-7ccc-8ccc-ccccccccccc1",
  blockers: [
    {
      code: "evaluation_already_pending",
      message:
        "An evaluation is already queued or running for this merchant. Wait for it to finish before starting another.",
    },
  ],
};

export const QUEUED_REEVALUATION_FIXTURE = {
  launch_id: "01a0cccc-cccc-7ccc-8ccc-ccccccccccc1",
  purpose: "REEVALUATION",
  status: "QUEUED",
  failure_code: null,
  requested_at: "2026-08-25T09:00:00Z",
  started_at: null,
  settled_at: null,
  representation_id: "01a00000-0000-7000-8000-000000000002",
  representation_label: "compiler:sha256:cfg000:sha256:ir0000",
  compiler_run_id: "01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1",
  source_snapshot_id: null,
  source_snapshot_label: null,
  suite_id: "01a00000-0000-7000-8000-000000000003",
  suite_label: "voltedge-core@2",
  mission_count: 14,
  environment_label: "voltedge-catalog@1 (fixture sha256:def456)",
  buyer_profile: "AI_BUYER",
  executor_kind: "llm-openai",
  provider: "openai-responses",
  requested_model: "gpt-5.6-terra",
  buyer_configuration_digest: "sha256:cfg000",
  run_id: null,
  run_status: null,
  missions_completed: null,
  baseline_run_id: "01992222-2222-7222-8222-222222222222",
  max_provider_requests: 252,
  provider_requests_charged: 0,
  provider_requests_remaining: 252,
  provider_requests_assumed_spent: 0,
  provider_responses: 0,
  unknown_usage_invocations: 0,
  comparison: null,
};

export const RUNNING_REEVALUATION_FIXTURE = {
  ...QUEUED_REEVALUATION_FIXTURE,
  status: "EXECUTING",
  started_at: "2026-08-25T09:00:20Z",
  run_id: "01a0dddd-dddd-7ddd-8ddd-ddddddddddd1",
  run_status: "RUNNING",
  missions_completed: 7,
  provider_requests_charged: 96,
  provider_requests_remaining: 156,
  provider_responses: 88,
  unknown_usage_invocations: 88,
  comparison: null,
};

/**
 * An evaluation AgentRank stopped because it had spent the model request allowance it was
 * launched with. Not a provider outage and not a catalog problem: the run is incomplete
 * because this deployment declined to buy more requests.
 */
export const BUDGET_EXHAUSTED_FIXTURE = {
  ...QUEUED_REEVALUATION_FIXTURE,
  status: "FAILED",
  failure_code: "provider_budget_exhausted",
  started_at: "2026-08-25T09:00:20Z",
  settled_at: "2026-08-25T09:12:40Z",
  run_id: "01a0dddd-dddd-7ddd-8ddd-ddddddddddd3",
  run_status: "ABORTED",
  missions_completed: 5,
  provider_requests_charged: 252,
  provider_requests_remaining: 0,
  provider_requests_assumed_spent: 24,
  provider_responses: 201,
  unknown_usage_invocations: 201,
  comparison: null,
};

/** An evaluation waiting because an operator paused model execution for its provider. */
export const PROVIDER_PAUSED_FIXTURE = {
  ...QUEUED_REEVALUATION_FIXTURE,
  status: "FAILED",
  failure_code: "provider_execution_paused",
  settled_at: "2026-08-25T09:01:00Z",
  comparison: null,
};

export const FAILED_REEVALUATION_FIXTURE = {
  ...QUEUED_REEVALUATION_FIXTURE,
  status: "FAILED",
  failure_code: "provider_credential_unavailable",
  settled_at: "2026-08-25T09:00:30Z",
  comparison: null,
};

/** A merchant's first evaluation, which has no artifact and no before. */
export const QUEUED_INITIAL_FIXTURE = {
  ...QUEUED_REEVALUATION_FIXTURE,
  launch_id: "01a0cccc-cccc-7ccc-8ccc-ccccccccccc2",
  purpose: "INITIAL",
  representation_id: null,
  representation_label: null,
  compiler_run_id: null,
  source_snapshot_id: "01a00000-0000-7000-8000-000000000001",
  source_snapshot_label: "voltedge-source@1",
  baseline_run_id: null,
};

export const COMPLETED_INITIAL_FIXTURE = {
  ...QUEUED_INITIAL_FIXTURE,
  status: "COMPLETED",
  started_at: "2026-08-25T09:00:20Z",
  settled_at: "2026-08-25T09:04:34Z",
  run_id: "01a0dddd-dddd-7ddd-8ddd-ddddddddddd2",
  run_status: "COMPLETED",
  missions_completed: 14,
  comparison: null,
};

export const COMPARISON_FIXTURE = {
  engine_identity: "sha256:engine0000",
  baseline_run_id: "01992222-2222-7222-8222-222222222222",
  candidate_run_id: "01a0dddd-dddd-7ddd-8ddd-ddddddddddd1",
  comparable: true,
  counts: [
    { key: "missions_total", before: 14, after: 14, delta: 0 },
    { key: "missions_succeeded", before: 6, after: 8, delta: 2 },
    { key: "unsafe_completions", before: 0, after: 0, delta: 0 },
    { key: "provider_failure_missions", before: 1, after: 0, delta: -1 },
  ],
  rates: [
    { key: "task_completion_rate", before: 0.75, after: 1.0, delta: 0.25 },
    { key: "correct_abstention_rate", before: 1.0, after: 1.0, delta: 0.0 },
  ],
  simulated_demand: [
    {
      currency: "EUR",
      bucket: "CAPTURED",
      simulated_before_amount_minor: 0,
      simulated_after_amount_minor: 0,
      simulated_delta_amount_minor: 0,
    },
    {
      currency: "INR",
      bucket: "CAPTURED",
      simulated_before_amount_minor: 2100000,
      simulated_after_amount_minor: 2939000,
      simulated_delta_amount_minor: 839000,
    },
  ],
  transitions: [
    {
      mission_key: "mission.usb-c.charger",
      before_status: "FAILED",
      before_primary_failure_reason: "ATTRIBUTE_MISSING",
      after_status: "SUCCEEDED",
      after_primary_failure_reason: null,
      direction: "IMPROVED",
    },
  ],
  interactions: {
    model_invocations: { key: "model_invocations", before: 40, after: 38, delta: -2 },
    tool_calls: { key: "tool_calls", before: 60, after: 57, delta: -3 },
    baseline_traced: true,
    candidate_traced: true,
    token_usage_complete: false,
  },
  baseline_runtime_seconds: 270.0,
  candidate_runtime_seconds: 254.0,
  warnings: [
    {
      code: "TOKEN_USAGE_UNAVAILABLE",
      message:
        "Some provider invocations reported no token usage, so interaction cost is compared through round trip and tool call counts only.",
    },
    {
      code: "SMALL_SAMPLE",
      message:
        "One run on each side. A single pair cannot separate a real change from ordinary variation between two executions.",
    },
    {
      code: "NOT_A_CONTROLLED_EXPERIMENT",
      message:
        "This is a before and after over time, not a controlled experiment. Anything that changed between the two runs is mixed into the difference, so read these numbers as an observation rather than as an effect of your representation.",
    },
  ],
  conclusion: {
    kind: "OUTCOME_DIFFERENCES",
    statement:
      "Between these two runs, 1 mission(s) ended differently (1 newly completed, 0 no longer completed); captured simulated demand changed (INR 2100000 to 2939000).",
  },
};

export const COMPLETED_REEVALUATION_FIXTURE = {
  ...QUEUED_REEVALUATION_FIXTURE,
  status: "COMPLETED",
  started_at: "2026-08-25T09:00:20Z",
  settled_at: "2026-08-25T09:04:34Z",
  run_id: "01a0dddd-dddd-7ddd-8ddd-ddddddddddd1",
  run_status: "COMPLETED",
  missions_completed: 14,
  comparison: COMPARISON_FIXTURE,
};

export const INCOMPARABLE_REEVALUATION_FIXTURE = {
  ...COMPLETED_REEVALUATION_FIXTURE,
  comparison: {
    ...COMPARISON_FIXTURE,
    comparable: false,
    conclusion: {
      kind: "INCOMPLETE",
      statement:
        "These two runs did not measure the same thing, so no before and after reading is offered.",
    },
    warnings: [
      {
        code: "SUITE_DIFFERS",
        message:
          "These runs executed different benchmark workloads, so their numbers are not measurements of the same thing.",
      },
      ...COMPARISON_FIXTURE.warnings,
    ],
  },
};
