/**
 * Test fixtures shaped exactly like the insights API's JSON responses.
 *
 * These exist so decoder and component tests exercise the wire shapes this console will
 * actually receive, not approximations of them. They are never imported by a route or a
 * shipped component, and nothing here is fake production data.
 */

export const OVERVIEW_FIXTURE = {
  engine_identity: "sha256:engine0000",
  merchant_id: "01991111-1111-7111-8111-111111111111",
  runs: [
    {
      run_id: "01992222-2222-7222-8222-222222222222",
      status: "COMPLETED",
      suite_label: "voltedge-core@2",
      executor_label: "gemini-flash-lite buyer v4",
      benchmark_designation: "DEVELOPMENT",
      started_at: "2026-08-23T10:00:00Z",
      completed_at: "2026-08-23T10:04:30Z",
      missions_total: 14,
      missions_succeeded: 8,
      missions_failed: 2,
      missions_abstained: 4,
      missions_errored: 0,
      purchase_missions: 8,
      control_missions: 4,
      correct_abstentions: 4,
      task_completion_rate: 1.0,
      correct_abstention_rate: 1.0,
      unsafe_attempts: 1,
      unsafe_completions: 0,
      provider_failure_missions: 1,
      simulated_demand: [
        {
          currency: "INR",
          simulated_potential_demand_amount_minor: 2939000,
          simulated_captured_demand_amount_minor: 2100000,
          simulated_lost_demand_amount_minor: 839000,
          simulated_not_measured_demand_amount_minor: 0,
        },
      ],
    },
    {
      run_id: "01993333-3333-7333-8333-333333333333",
      status: "RUNNING",
      suite_label: "voltedge-core@2",
      executor_label: null,
      benchmark_designation: null,
      started_at: null,
      completed_at: null,
      missions_total: 14,
      missions_succeeded: 3,
      missions_failed: 1,
      missions_abstained: 2,
      missions_errored: 0,
      purchase_missions: 8,
      control_missions: 4,
      correct_abstentions: 4,
      task_completion_rate: null,
      correct_abstention_rate: null,
      unsafe_attempts: 0,
      unsafe_completions: 0,
      provider_failure_missions: 0,
      simulated_demand: [],
    },
  ],
  top_findings: [
    {
      key: "ATTRIBUTE_NOT_PUBLISHED:wattage",
      code: "ATTRIBUTE_NOT_PUBLISHED",
      owner: "MERCHANT_CATALOG",
      actionability: "MERCHANT_ACTION",
      severity: "MEDIUM",
      evidence_level: "TRUSTED_FACT",
      title:
        "2 purchase missions could not verify a required wattage attribute in your catalog data.",
      recommendation: "Publish the missing attribute values for the affected variants.",
      mission_run_ids: [
        "01994444-4444-7444-8444-444444444444",
        "01995555-5555-7555-8555-555555555555",
      ],
      mission_keys: ["mission.usb-c.charger", "mission.travel.charger"],
      product_ids: ["01996666-6666-7666-8666-666666666666"],
      variant_ids: ["01997777-7777-7777-8777-777777777777"],
      attribute_keys: ["wattage"],
      simulated_demand: [{ currency: "INR", bucket: "AT_RISK", simulated_amount_minor: 419900 }],
      compiler_references: [
        {
          compiler_run_id: "01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1",
          candidate_id: "01a0bbbb-bbbb-7bbb-8bbb-bbbbbbbbbbb1",
          target: "variant.VE-CHG-100-BLK.attribute.wattage",
        },
      ],
    },
    {
      key: "PROVIDER_OUTAGE_TERMINATED_MISSION:01998888",
      code: "PROVIDER_OUTAGE_TERMINATED_MISSION",
      owner: "MODEL_PROVIDER",
      actionability: "NO_MERCHANT_ACTION",
      severity: "HIGH",
      evidence_level: "DETERMINISTIC_ATTRIBUTION",
      title:
        "1 benchmark mission ended on a model provider outage. No action is required from you.",
      recommendation: null,
      mission_run_ids: ["01999999-9999-7999-8999-999999999999"],
      mission_keys: ["mission.cable.pack"],
      product_ids: [],
      variant_ids: [],
      attribute_keys: [],
      simulated_demand: [],
      compiler_references: [],
    },
  ],
  top_findings_run_id: "01992222-2222-7222-8222-222222222222",
  simulated_demand_totals_by_currency: [
    {
      currency: "EUR",
      simulated_potential_demand_amount_minor: 49900,
      simulated_captured_demand_amount_minor: 0,
      simulated_lost_demand_amount_minor: 49900,
      simulated_not_measured_demand_amount_minor: 0,
    },
    {
      currency: "INR",
      simulated_potential_demand_amount_minor: 2939000,
      simulated_captured_demand_amount_minor: 2100000,
      simulated_lost_demand_amount_minor: 839000,
      simulated_not_measured_demand_amount_minor: 0,
    },
  ],
  latest_experiment: {
    experiment_id: "01a02f08-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
    benchmark_designation: "EVALUATION",
    completed_sample_pairs: 2,
    conclusion_kind: "PARITY",
    conclusion_statement: "No measurable compiler benefit was observed at this sample size.",
    warnings: [
      {
        code: "SMALL_SAMPLE",
        message:
          "2 completed pair(s). A single paired sample cannot distinguish treatment effects from ordinary variation.",
      },
    ],
  },
  representation_state: {
    source_snapshot_id: "01a00000-0000-7000-8000-000000000001",
    source_snapshot_label: "voltedge storefront snapshot",
    compiled_representation_id: "01a00000-0000-7000-8000-000000000002",
    compiled_representation_label: "VoltEdge agent-ready IR",
    review_required_facts: 3,
  },
};

export const RUN_DIAGNOSTICS_FIXTURE = {
  engine_identity: "sha256:engine0000",
  run_id: "01992222-2222-7222-8222-222222222222",
  status: "COMPLETED",
  suite_label: "voltedge-core@2",
  environment_label: "voltedge-catalog@1 (fixture sha256:abc123)",
  representation_id: "01a00000-0000-7000-8000-000000000002",
  representation_label: "VoltEdge agent-ready IR",
  compiler_run_id: "01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1",
  catalog_hash: "sha256:def456",
  evaluator_version: "vocabulary sha256:789",
  executor_label: "gemini-flash-lite buyer v4",
  executor_revision: "sha256:rev000",
  agent_implementation_version: 4,
  benchmark_designation: "DEVELOPMENT",
  created_at: "2026-08-23T09:59:00Z",
  started_at: "2026-08-23T10:00:00Z",
  completed_at: "2026-08-23T10:04:30Z",
  catalog_pin_verified: true,
  metrics: {
    missions_total: 14,
    missions_succeeded: 8,
    missions_failed: 2,
    missions_abstained: 4,
    missions_errored: 0,
    missions_unfinished: 0,
    purchase_missions: 8,
    control_missions: 6,
    correct_abstentions: 4,
    incorrect_abstentions: 0,
    task_completion_rate: 1.0,
    correct_abstention_rate: 0.6666666666666666,
    unsafe_attempts: 1,
    unverified_attempts: 0,
    unsafe_completions: 0,
    mandate_denials_protecting: 1,
    mandate_denials_on_compliant_attempt: 0,
    oracle_disagreements: 0,
    oracle_unchecked: 0,
    primary_failure_counts: {
      ATTRIBUTE_NOT_PUBLISHED: 1,
      AGENT_EXECUTION_ERROR: 1,
    },
  },
  findings: OVERVIEW_FIXTURE.top_findings,
  missions: [
    {
      engine_identity: "sha256:engine0000",
      run_id: "01992222-2222-7222-8222-222222222222",
      mission_run_id: "01994444-4444-7444-8444-444444444444",
      mission_key: "mission.usb-c.charger",
      status: "FAILED",
      outcome: "The mission failed before producing a purchasable offer.",
      primary_code: "ATTRIBUTE_NOT_PUBLISHED",
      findings: [
        {
          code: "ATTRIBUTE_NOT_PUBLISHED",
          owner: "MERCHANT_CATALOG",
          actionability: "MERCHANT_ACTION",
          severity: "MEDIUM",
          evidence_level: "TRUSTED_FACT",
          summary:
            "The buyer could not establish the required wattage attribute for the selected variant.",
          recommendation: "Publish wattage values for these variants in your catalog data.",
          attribute_keys: ["wattage"],
          product_ids: ["01996666-6666-7666-8666-666666666666"],
          variant_ids: ["01997777-7777-7777-8777-777777777777"],
          evidence: [
            {
              kind: "mission_result",
              identifier: "01994444-4444-7444-8444-444444444444",
              establishes: "the recorded failure reason and its attributes",
            },
            {
              kind: "variant",
              identifier: "01997777-7777-7777-8777-777777777777",
              establishes: "the selected variant the diagnosis names",
            },
          ],
          compiler_references: [
            {
              compiler_run_id: "01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1",
              candidate_id: "01a0bbbb-bbbb-7bbb-8bbb-bbbbbbbbbbb1",
              target: "variant.VE-CHG-100-BLK.attribute.wattage",
            },
          ],
        },
        {
          code: "PROVIDER_THROTTLE_RECOVERED",
          owner: "MODEL_PROVIDER",
          actionability: "NO_MERCHANT_ACTION",
          severity: "LOW",
          evidence_level: "TRUSTED_FACT",
          summary:
            "One throttled model provider invocation recovered within the mission. Operational history only.",
          recommendation: null,
          attribute_keys: [],
          product_ids: [],
          variant_ids: [],
          evidence: [
            {
              kind: "trace_event",
              identifier: "evt-17",
              establishes: "a throttle that was retried and recovered",
            },
          ],
          compiler_references: [],
        },
      ],
      simulated_demand: [{ currency: "INR", bucket: "AT_RISK", simulated_amount_minor: 259900 }],
      model_invocations: 5,
      tool_calls: 9,
      tool_errors: 1,
    },
    {
      engine_identity: "sha256:engine0000",
      run_id: "01992222-2222-7222-8222-222222222222",
      mission_run_id: "01999999-9999-7999-8999-999999999999",
      mission_key: "mission.cable.pack",
      status: "FAILED",
      outcome: "The mission produced no usable outcome.",
      primary_code: "PROVIDER_OUTAGE_TERMINATED_MISSION",
      findings: [
        {
          code: "PROVIDER_OUTAGE_TERMINATED_MISSION",
          owner: "MODEL_PROVIDER",
          actionability: "NO_MERCHANT_ACTION",
          severity: "HIGH",
          evidence_level: "DETERMINISTIC_ATTRIBUTION",
          summary:
            "The model provider never produced a usable response and the mission ended on it.",
          recommendation: null,
          attribute_keys: [],
          product_ids: [],
          variant_ids: [],
          evidence: [
            {
              kind: "trace_event",
              identifier: "evt-31",
              establishes: "a terminating provider failure",
            },
          ],
          compiler_references: [],
        },
      ],
      simulated_demand: [],
      model_invocations: 2,
      tool_calls: 0,
      tool_errors: 0,
    },
    {
      engine_identity: "sha256:engine0000",
      run_id: "01992222-2222-7222-8222-222222222222",
      mission_run_id: "01aa0000-0000-7000-8000-00000000eeee",
      mission_key: "mission.control.abstain",
      status: "ABSTAINED",
      outcome: "The buyer correctly declined to purchase.",
      primary_code: null,
      findings: [],
      simulated_demand: [],
      model_invocations: 3,
      tool_calls: 4,
      tool_errors: 0,
    },
  ],
  provider_health: {
    missions_with_provider_errors: 2,
    terminated_outages: 1,
    recovered_throttles: 1,
    requested_model: "gemini-3.5-flash-lite",
    resolved_models: ["gemini-3.5-flash-lite"],
  },
  simulated_demand: [
    {
      currency: "INR",
      simulated_potential_demand_amount_minor: 2939000,
      simulated_captured_demand_amount_minor: 2100000,
      simulated_lost_demand_amount_minor: 839000,
      simulated_not_measured_demand_amount_minor: 0,
    },
  ],
};

export const TRACE_FIXTURE = {
  total_events: 6,
  events: [
    {
      sequence: 1,
      event_type: "MODEL_REQUEST",
      recorded_at: "2026-08-23T10:00:05Z",
      payload: { model: "gemini-3.5-flash-lite", turn: 1 },
    },
    {
      sequence: 2,
      event_type: "MODEL_RESPONSE",
      recorded_at: "2026-08-23T10:00:07Z",
      payload: { model: "gemini-3.5-flash-lite", usage: { prompt_tokens: "[redacted]" } },
    },
    {
      sequence: 3,
      event_type: "TOOL_CALL",
      recorded_at: "2026-08-23T10:00:08Z",
      payload: { tool: "search_products", arguments: { query: "usb c charger" } },
    },
    {
      sequence: 4,
      event_type: "TOOL_RESULT",
      recorded_at: "2026-08-23T10:00:09Z",
      payload: {
        tool: "search_products",
        result: { truncated: true, sha256_length: 9000, preview: '{"products":' },
      },
    },
    {
      sequence: 5,
      event_type: "TOOL_ERROR",
      recorded_at: "2026-08-23T10:00:12Z",
      payload: { tool: "prepare_execution", error_kind: "InsufficientInventory" },
    },
    {
      sequence: 6,
      event_type: "PROVIDER_ERROR",
      recorded_at: "2026-08-23T10:00:20Z",
      payload: { kind: "ProviderThrottledError", detail: "429 rate limited, retrying" },
    },
  ],
};

const EXPERIMENT_ARM = (
  arm: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> => ({
  arm,
  planned_samples: 2,
  completed_samples: 2,
  completion_rate_mean: 1.0,
  terminated_provider_outages: 0,
  missions_with_provider_errors: 0,
  model_invocations: 40,
  tool_calls: 120,
  resolved_models: ["gemini-3.5-flash-lite"],
  metrics_totals: {
    missions_total: 36,
    missions_succeeded: 20,
    missions_failed: 0,
    missions_abstained: 16,
    missions_errored: 0,
    missions_unfinished: 0,
    purchase_missions: 20,
    control_missions: 16,
    correct_abstentions: 16,
    incorrect_abstentions: 0,
    task_completion_rate: 1.0,
    correct_abstention_rate: 1.0,
    unsafe_attempts: 0,
    unverified_attempts: 0,
    unsafe_completions: 0,
    mandate_denials_protecting: 0,
    mandate_denials_on_compliant_attempt: 0,
    oracle_disagreements: 0,
    oracle_unchecked: 0,
    primary_failure_counts: {},
  },
  ...overrides,
});

export const EXPERIMENT_PARITY_FIXTURE = {
  engine_identity: "sha256:engine0000",
  experiment_id: "01a02f08-aaaa-7aaa-8aaa-aaaaaaaaaaaa",
  buyer_configuration_digest: "sha256:buyer000",
  benchmark_designation: "EVALUATION",
  pair_order: "counterbalanced",
  declared_sample_pairs: 2,
  completed_sample_pairs: 2,
  arms: [EXPERIMENT_ARM("RAW"), EXPERIMENT_ARM("COMPILED")],
  demand_delta_by_currency: [
    {
      currency: "INR",
      simulated_potential_delta_amount_minor: 0,
      simulated_captured_delta_amount_minor: 0,
      simulated_lost_delta_amount_minor: 0,
      simulated_not_measured_delta_amount_minor: 0,
    },
  ],
  mission_transitions: [],
  warnings: [
    {
      code: "SMALL_SAMPLE",
      message:
        "2 completed pair(s). A single paired sample cannot distinguish treatment effects from ordinary variation.",
    },
  ],
  conclusion: {
    kind: "PARITY",
    statement: "No measurable compiler benefit was observed at this sample size.",
  },
};

export const EXPERIMENT_DIFFERENCES_FIXTURE = {
  ...EXPERIMENT_PARITY_FIXTURE,
  arms: [
    EXPERIMENT_ARM("RAW"),
    EXPERIMENT_ARM("COMPILED", {
      completion_rate_mean: 0.75,
      metrics_totals: {
        missions_total: 36,
        missions_succeeded: 15,
        missions_failed: 5,
        missions_abstained: 16,
        missions_errored: 0,
        missions_unfinished: 0,
        purchase_missions: 20,
        control_missions: 16,
        correct_abstentions: 16,
        incorrect_abstentions: 0,
        task_completion_rate: 0.75,
        correct_abstention_rate: 1.0,
        unsafe_attempts: 0,
        unverified_attempts: 0,
        unsafe_completions: 0,
        mandate_denials_protecting: 0,
        mandate_denials_on_compliant_attempt: 0,
        oracle_disagreements: 0,
        oracle_unchecked: 0,
        primary_failure_counts: { STOCK_UNAVAILABLE: 5 },
      },
    }),
  ],
  demand_delta_by_currency: [
    {
      currency: "INR",
      simulated_potential_delta_amount_minor: 0,
      simulated_captured_delta_amount_minor: -739100,
      simulated_lost_delta_amount_minor: 739100,
      simulated_not_measured_delta_amount_minor: 0,
    },
  ],
  mission_transitions: [
    {
      pair_ordinal: 1,
      mission_key: "mission.travel.charger",
      raw_status: "SUCCEEDED",
      raw_primary_failure_reason: null,
      compiled_status: "FAILED",
      compiled_primary_failure_reason: "STOCK_UNAVAILABLE",
      direction: "COMPILED_LOSS",
    },
  ],
  warnings: [
    {
      code: "DEVELOPMENT_BENCHMARK",
      message:
        "This experiment ran on a development benchmark; it is not independent evaluation evidence.",
    },
    {
      code: "SMALL_SAMPLE",
      message:
        "2 completed pair(s). A single paired sample cannot distinguish treatment effects from ordinary variation.",
    },
  ],
  conclusion: {
    kind: "OUTCOME_DIFFERENCES",
    statement:
      "The arms disagreed on 1 measured mission, so the representations did not perform identically at this sample size.",
  },
};

/** A mutable view of a fixture for tests that need to vary one field. */
export type Mutable<T> = {
  -readonly [K in keyof T]: T[K] extends readonly (infer Item)[]
    ? Mutable<Item>[]
    : T[K] extends object | null
      ? T[K] extends null
        ? T[K]
        : Mutable<NonNullable<T[K]>> | null
      : T[K];
};
