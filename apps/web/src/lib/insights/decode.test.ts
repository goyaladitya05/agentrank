import { describe, expect, it } from "vitest";

import {
  decodeExperimentComparison,
  decodeMerchantOverview,
  decodeRunDiagnostics,
  decodeRunSummary,
  decodeTraceProjection,
  DecodeError,
} from "./decode";
import {
  EXPERIMENT_DIFFERENCES_FIXTURE,
  EXPERIMENT_PARITY_FIXTURE,
  OVERVIEW_FIXTURE,
  RUN_DIAGNOSTICS_FIXTURE,
  TRACE_FIXTURE,
} from "./fixtures";

describe("insight decoders", () => {
  it("decodes a full overview payload", () => {
    const overview = decodeMerchantOverview(OVERVIEW_FIXTURE);
    expect(overview.merchant_id).toBe("01991111-1111-7111-8111-111111111111");
    expect(overview.runs).toHaveLength(2);
    expect(overview.top_findings[0]?.owner).toBe("MERCHANT_CATALOG");
    expect(overview.top_findings[1]?.actionability).toBe("NO_MERCHANT_ACTION");
    expect(overview.latest_experiment?.conclusion_kind).toBe("PARITY");
    expect(overview.representation_state.review_required_facts).toBe(3);
    expect(overview.simulated_demand_totals_by_currency.map((bucket) => bucket.currency)).toEqual([
      "EUR",
      "INR",
    ]);
  });

  it("decodes an overview whose latest experiment is null", () => {
    const payload = { ...OVERVIEW_FIXTURE, latest_experiment: null };
    expect(decodeMerchantOverview(payload).latest_experiment).toBeNull();
  });

  it("decodes full run diagnostics", () => {
    const run = decodeRunDiagnostics(RUN_DIAGNOSTICS_FIXTURE);
    expect(run.benchmark_designation).toBe("DEVELOPMENT");
    expect(run.missions).toHaveLength(3);
    expect(run.provider_health.terminated_outages).toBe(1);
    expect(run.metrics.primary_failure_counts["ATTRIBUTE_NOT_PUBLISHED"]).toBe(1);
    expect(run.simulated_demand[0]?.simulated_captured_demand_amount_minor).toBe(2100000);
  });

  it("decodes a trace projection with redacted payloads intact", () => {
    const trace = decodeTraceProjection(TRACE_FIXTURE);
    expect(trace.total_events).toBe(6);
    expect(trace.events.map((event) => event.event_type)).toEqual([
      "MODEL_REQUEST",
      "MODEL_RESPONSE",
      "TOOL_CALL",
      "TOOL_RESULT",
      "TOOL_ERROR",
      "PROVIDER_ERROR",
    ]);
    const toolResult = trace.events[3]?.payload;
    const nested = toolResult?.["result"];
    expect(nested).toHaveProperty("truncated", true);
  });

  it("decodes experiment comparisons in both conclusion shapes", () => {
    const parity = decodeExperimentComparison(EXPERIMENT_PARITY_FIXTURE);
    expect(parity.conclusion.kind).toBe("PARITY");
    expect(parity.arms).toHaveLength(2);
    expect(parity.mission_transitions).toHaveLength(0);

    const differences = decodeExperimentComparison(EXPERIMENT_DIFFERENCES_FIXTURE);
    expect(differences.conclusion.kind).toBe("OUTCOME_DIFFERENCES");
    expect(differences.demand_delta_by_currency[0]?.simulated_captured_delta_amount_minor).toBe(
      -739100,
    );
    expect(differences.warnings.map((warning) => warning.code)).toEqual([
      "DEVELOPMENT_BENCHMARK",
      "SMALL_SAMPLE",
    ]);
  });

  it("rejects a run summary whose counts are not integers", () => {
    const payload = {
      ...OVERVIEW_FIXTURE.runs[0],
      missions_total: "fourteen",
    };
    expect(() => decodeRunSummary(payload)).toThrow(DecodeError);
    expect(() => decodeRunSummary(payload)).toThrow(/missions_total/);
  });

  it("rejects unknown diagnostic owners instead of rendering them as known labels", () => {
    const finding = {
      ...OVERVIEW_FIXTURE.top_findings[0],
      owner: "SOMEONE_ELSE",
    };
    expect(() => decodeMerchantOverview({ ...OVERVIEW_FIXTURE, top_findings: [finding] })).toThrow(
      /owner/,
    );
  });

  it("names the offending field when a required field is missing", () => {
    const partial = { ...RUN_DIAGNOSTICS_FIXTURE } as Record<string, unknown>;
    delete partial["provider_health"];
    expect(() => decodeRunDiagnostics(partial)).toThrow();
  });

  it("refuses arrays and scalars where an object is expected", () => {
    expect(() => decodeMerchantOverview([OVERVIEW_FIXTURE])).toThrow(DecodeError);
    expect(() => decodeMerchantOverview(null)).toThrow(DecodeError);
    expect(() => decodeRunSummary("run")).toThrow(DecodeError);
  });
});
