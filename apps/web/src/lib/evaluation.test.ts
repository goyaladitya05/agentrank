import { describe, expect, it } from "vitest";

import { DecodeError } from "@/lib/insights/decode";
import {
  decodePreflight,
  decodeEvaluationLaunch,
  decodeEvaluationLaunchDetail,
  decodeEvaluationLaunchList,
} from "@/lib/evaluation";
import {
  BLOCKED_PREFLIGHT_FIXTURE,
  COMPLETED_REEVALUATION_FIXTURE,
  PREFLIGHT_FIXTURE,
  QUEUED_REEVALUATION_FIXTURE,
  REFERENCE_PREFLIGHT_FIXTURE,
} from "@/lib/evaluation-fixtures";

describe("preflight decoding", () => {
  it("keeps every field the merchant is shown before spending a run", () => {
    const preflight = decodePreflight(PREFLIGHT_FIXTURE);
    expect(preflight.launchable).toBe(true);
    expect(preflight.mission_count).toBe(14);
    expect(preflight.requested_model).toBe("gpt-5.6-terra");
    expect(preflight.blockers).toEqual([]);
  });

  it("keeps a buyer with no provider as nulls rather than inventing bounds", () => {
    const preflight = decodePreflight(REFERENCE_PREFLIGHT_FIXTURE);
    expect(preflight.buyer_profile).toBe("REFERENCE_BUYER");
    expect(preflight.provider).toBeNull();
    expect(preflight.max_model_turns).toBeNull();
    expect(preflight.mission_deadline_seconds).toBeNull();
  });

  it("carries every blocker with its own sentence", () => {
    const preflight = decodePreflight(BLOCKED_PREFLIGHT_FIXTURE);
    expect(preflight.launchable).toBe(false);
    expect(preflight.blockers[0]?.code).toBe("evaluation_already_pending");
    expect(preflight.pending_launch_id).not.toBeNull();
  });

  it("refuses a buyer profile this console does not know", () => {
    expect(() => decodePreflight({ ...PREFLIGHT_FIXTURE, buyer_profile: "MAGIC" })).toThrow(
      DecodeError,
    );
  });
});

describe("launch decoding", () => {
  it("keeps a queued launch free of any run fact", () => {
    const launch = decodeEvaluationLaunch(QUEUED_REEVALUATION_FIXTURE);
    expect(launch.status).toBe("QUEUED");
    expect(launch.run_id).toBeNull();
    expect(launch.missions_completed).toBeNull();
  });

  it("decodes a list", () => {
    expect(decodeEvaluationLaunchList([QUEUED_REEVALUATION_FIXTURE])).toHaveLength(1);
  });

  it("refuses an unknown status rather than passing it through", () => {
    expect(() => decodeEvaluationLaunch({ ...QUEUED_REEVALUATION_FIXTURE, status: "DONE" })).toThrow(
      DecodeError,
    );
  });

  it("decodes a comparison with its caveats and per currency demand", () => {
    const detail = decodeEvaluationLaunchDetail(COMPLETED_REEVALUATION_FIXTURE);
    expect(detail.comparison).not.toBeNull();
    const comparison = detail.comparison;
    if (comparison === null) throw new Error("expected a comparison");
    expect(comparison.comparable).toBe(true);
    expect(comparison.warnings.map((warning) => warning.code)).toContain(
      "NOT_A_CONTROLLED_EXPERIMENT",
    );
    expect(new Set(comparison.simulated_demand.map((change) => change.currency))).toEqual(
      new Set(["EUR", "INR"]),
    );
    expect(comparison.interactions.token_usage_complete).toBe(false);
  });

  it("keeps an absent comparison null rather than an empty one", () => {
    expect(decodeEvaluationLaunchDetail(QUEUED_REEVALUATION_FIXTURE).comparison).toBeNull();
  });
});
