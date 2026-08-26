import { describe, expect, it } from "vitest";

import { decodePreflight, type RunComparison } from "@/lib/evaluation";
import {
  BLOCKED_PREFLIGHT_FIXTURE,
  INITIAL_BLOCKED_PREFLIGHT_FIXTURE,
  INITIAL_PREFLIGHT_FIXTURE,
  PREFLIGHT_FIXTURE,
} from "@/lib/evaluation-fixtures";
import { decodeMerchantOverview } from "@/lib/insights/decode";
import { OVERVIEW_FIXTURE, type Mutable } from "@/lib/insights/fixtures";
import { compareSummary, composeHistory, groupFindings, nextAction } from "@/lib/insights/merchant";
import type { MerchantOverview } from "@/lib/insights/types";

function overviewWith(mutate: (overview: Mutable<MerchantOverview>) => void): MerchantOverview {
  const clone = structuredClone(OVERVIEW_FIXTURE) as unknown as MerchantOverview;
  mutate(clone as Mutable<MerchantOverview>);
  return decodeMerchantOverview(clone);
}

/** The fixture's representation state, which the fixture always carries. */
function repState(data: Mutable<MerchantOverview>) {
  if (data.representation_state === null)
    throw new Error("the fixture has no representation state");
  return data.representation_state;
}

describe("groupFindings", () => {
  it("splits by the API's actionability and never moves a provider finding", () => {
    const overview = overviewWith(() => undefined);
    const grouped = groupFindings(overview.top_findings);
    expect(grouped.needsAttention.map((finding) => finding.code)).toEqual([
      "ATTRIBUTE_NOT_PUBLISHED",
    ]);
    expect(grouped.noActionRequired.map((finding) => finding.code)).toEqual([
      "PROVIDER_OUTAGE_TERMINATED_MISSION",
    ]);
  });

  it("orders attention by severity, most severe first", () => {
    const overview = overviewWith((data) => {
      const merchantFinding = structuredClone(
        data.top_findings[0],
      ) as Mutable<MerchantOverview>["top_findings"][number];
      merchantFinding.key = "SECOND:critical";
      merchantFinding.severity = "CRITICAL";
      data.top_findings = [...data.top_findings, merchantFinding];
    });
    const grouped = groupFindings(overview.top_findings);
    expect(grouped.needsAttention.map((finding) => finding.severity)).toEqual([
      "CRITICAL",
      "MEDIUM",
    ]);
  });
});

describe("nextAction", () => {
  const reeval = decodePreflight(PREFLIGHT_FIXTURE);
  const initial = decodePreflight(INITIAL_PREFLIGHT_FIXTURE);

  it("puts a pending evaluation above everything", () => {
    const pending = decodePreflight(BLOCKED_PREFLIGHT_FIXTURE);
    const action = nextAction(
      overviewWith(() => undefined),
      pending,
    );
    expect(action.kind).toBe("evaluation-in-progress");
    expect(action.href).toContain(String(BLOCKED_PREFLIGHT_FIXTURE.pending_launch_id));
  });

  it("sends a merchant with no source and no runs to the import", () => {
    const overview = overviewWith((data) => {
      data.runs = [];
      data.top_findings = [];
      repState(data).source_snapshot_id = null;
      repState(data).compiled_representation_id = null;
      repState(data).review_required_facts = 0;
    });
    const action = nextAction(overview, decodePreflight(INITIAL_BLOCKED_PREFLIGHT_FIXTURE));
    expect(action.kind).toBe("import-store");
    expect(action.href).toBe("/sources/import");
  });

  it("sends a merchant with a source and no finished run to the first evaluation", () => {
    const overview = overviewWith((data) => {
      data.runs = [];
      data.top_findings = [];
    });
    expect(nextAction(overview, initial).kind).toBe("run-first-evaluation");
  });

  it("prefers reviewing proposed fixes over rereading issues", () => {
    // The fixture carries a merchant finding and three facts awaiting review; the concrete
    // decision the merchant can make right now is the facts.
    const action = nextAction(
      overviewWith(() => undefined),
      reeval,
    );
    expect(action.kind).toBe("review-fixes");
    expect(action.label).toBe("Review 3 fixes");
  });

  it("sends merchant findings with nothing to review to the issues page", () => {
    const overview = overviewWith((data) => {
      repState(data).review_required_facts = 0;
    });
    expect(nextAction(overview, null).kind).toBe("review-issues");
  });

  it("offers measure again once fixes are published and reviewed", () => {
    const overview = overviewWith((data) => {
      repState(data).review_required_facts = 0;
      data.top_findings = [];
    });
    expect(nextAction(overview, reeval).kind).toBe("measure-again");
  });
});

describe("composeHistory", () => {
  const launch = {
    launch_id: "01a0cccc-cccc-7ccc-8ccc-ccccccccccc1",
    purpose: "INITIAL",
    status: "COMPLETED",
    failure_code: null,
    requested_at: "2026-08-23T09:00:00Z",
    started_at: "2026-08-23T09:01:00Z",
    settled_at: "2026-08-23T09:10:00Z",
    representation_id: null,
    representation_label: null,
    compiler_run_id: null,
    source_snapshot_id: "01a00000-0000-7000-8000-000000000001",
    source_snapshot_label: "source@1",
    suite_id: "01a00000-0000-7000-8000-000000000003",
    suite_label: "voltedge-core@2",
    mission_count: 14,
    environment_label: "voltedge-catalog@1",
    buyer_profile: "AI_BUYER",
    executor_kind: "llm-openai",
    provider: "openai-responses",
    requested_model: "gpt-5.6-terra",
    buyer_configuration_digest: null,
    run_id: null,
    run_status: null,
    missions_completed: 14,
    baseline_run_id: null,
    max_provider_requests: null,
    provider_requests_charged: null,
    provider_requests_remaining: null,
    provider_requests_assumed_spent: null,
    provider_responses: null,
    unknown_usage_invocations: null,
  } as const;
  const snapshot = {
    source_snapshot_id: "01a00000-0000-7000-8000-000000000001",
    source_label: "source@1",
    origin: "MERCHANT_IMPORT",
    created_at: "2026-08-22T08:00:00Z",
    product_count: 4,
    variant_count: 9,
  };
  const compilerRun = {
    run_id: "01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1",
    source_label: "source@1",
    status: "COMPLETED",
    created_at: "2026-08-24T12:00:00Z",
    review_required_count: 4,
    reviewed_count: 4,
    published_representation_id: "01a00000-0000-7000-8000-000000000002",
  };

  it("streams evaluations, source updates and fix batches newest first", () => {
    const events = composeHistory([launch], [snapshot], [compilerRun]);
    expect(events.map((event) => event.title)).toEqual([
      "Fixes published",
      "First evaluation",
      "Store imported",
    ]);
  });

  it("gives every event a merchant sentence and a place to go", () => {
    const events = composeHistory([launch], [snapshot], [compilerRun]);
    expect(events[1]?.detail).toBe("14 of 14 shopping scenarios executed.");
    expect(events[1]?.href).toBe("/evaluations/01a0cccc-cccc-7ccc-8ccc-ccccccccccc1");
    expect(events[2]?.detail).toContain("4 products");
  });

  it("reports a withdrawn launch as withdrawn, never as a failure", () => {
    const withdrawn = {
      ...launch,
      status: "FAILED",
      failure_code: "withdrawn_by_merchant",
    } as const;
    const events = composeHistory([withdrawn], [], []);
    expect(events[0]?.status.label).toBe("Withdrawn");
    expect(events[0]?.status.tone).toBe("neutral");
    expect(events[0]?.detail).toContain("Nothing was measured");
  });

  it("keeps an unpublished batch honest about what still waits", () => {
    const waiting = { ...compilerRun, reviewed_count: 1, published_representation_id: null };
    const events = composeHistory([], [], [waiting]);
    expect(events[0]?.title).toBe("Fixes proposed");
    expect(events[0]?.detail).toBe("3 facts wait for your review.");
  });
});

describe("compareSummary", () => {
  const comparison: RunComparison = {
    engine_identity: "sha256:engine0000",
    baseline_run_id: "01992222-2222-7222-8222-222222222222",
    candidate_run_id: "01993333-3333-7333-8333-333333333333",
    comparable: true,
    counts: [
      { key: "missions_succeeded", before: 18, after: 22, delta: 4 },
      { key: "purchase_missions", before: 24, after: 24, delta: 0 },
    ],
    rates: [],
    simulated_demand: [
      {
        currency: "INR",
        bucket: "CAPTURED",
        simulated_before_amount_minor: 2100000,
        simulated_after_amount_minor: 2519900,
        simulated_delta_amount_minor: 419900,
      },
    ],
    transitions: [
      {
        mission_key: "mission.a",
        before_status: "FAILED",
        before_primary_failure_reason: "ATTRIBUTE_MISSING",
        after_status: "SUCCEEDED",
        after_primary_failure_reason: null,
        direction: "IMPROVED",
      },
    ],
    interactions: {
      model_invocations: null,
      tool_calls: null,
      baseline_traced: false,
      candidate_traced: false,
      token_usage_complete: null,
    },
    baseline_runtime_seconds: null,
    candidate_runtime_seconds: null,
    warnings: [],
    conclusion: { kind: "OUTCOME_DIFFERENCES", statement: "Outcomes differ." },
  };

  it("summarizes the payoff from the engine's own counts and transitions", () => {
    const summary = compareSummary(comparison);
    expect(summary).not.toBeNull();
    expect(summary?.succeededBefore).toBe(18);
    expect(summary?.succeededAfter).toBe(22);
    expect(summary?.improved).toBe(1);
    expect(summary?.regressed).toBe(0);
    expect(summary?.capturedDemand).toEqual([
      { currency: "INR", beforeMinor: 2100000, afterMinor: 2519900 },
    ]);
  });

  it("refuses to summarize what the engine said is not comparable", () => {
    expect(compareSummary({ ...comparison, comparable: false })).toBeNull();
  });
});
