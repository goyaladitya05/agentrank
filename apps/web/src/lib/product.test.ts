import { describe, expect, it } from "vitest";

import {
  actionabilityLabel,
  actionabilityTone,
  conclusionKindLabel,
  designationLabel,
  demandBucketLabel,
  evidenceLevelLabel,
  humanize,
  ownerLabel,
  severityTone,
  statusLabel,
  traceEventLabel,
  transitionDirectionLabel,
} from "./labels";
import { formatMoney, formatRate, formatSignedMoney, formatTimestamp } from "./format";

describe("ownership and actionability vocabulary", () => {
  it("speaks to the merchant, never in enum codes", () => {
    expect(ownerLabel("MERCHANT_CATALOG")).toBe("Your catalog");
    expect(ownerLabel("MODEL_PROVIDER")).toBe("Model provider");
    expect(ownerLabel("UNKNOWN")).toBe("Unresolved");
    expect(actionabilityLabel("MERCHANT_ACTION")).toBe("You can fix this");
    expect(actionabilityLabel("NO_MERCHANT_ACTION")).toBe("No action required from you");
  });

  it("keeps provider issues visually calm and merchant issues visible", () => {
    expect(actionabilityTone("MERCHANT_ACTION")).toBe("warn");
    expect(actionabilityTone("NO_MERCHANT_ACTION")).toBe("neutral");
    expect(actionabilityTone("REVIEW_REQUIRED")).toBe("info");
  });

  it("falls back to readable text for values the console does not know yet", () => {
    expect(ownerLabel("SOMETHING_NEW")).toBe("Something new");
    expect(severityTone("WHATEVER")).toBe("neutral");
    expect(evidenceLevelLabel("FUTURE_LEVEL")).toBe("Future level");
  });
});

describe("status and designation vocabulary", () => {
  it("labels statuses with tones that do not overclaim", () => {
    expect(statusLabel("COMPLETED").tone).toBe("ok");
    expect(statusLabel("ERRORED")).toEqual({ label: "Errored", tone: "neutral" });
    expect(statusLabel("FAILED").label).toBe("Failed");
  });

  it("marks development benchmarks as not evaluation evidence", () => {
    const development = designationLabel("DEVELOPMENT");
    expect(development.label).toBe("Development benchmark");
    expect(development.note).toMatch(/Not independent evaluation evidence/);
    const evaluation = designationLabel("EVALUATION");
    expect(evaluation.label).toBe("Evaluation benchmark");
    const unrecorded = designationLabel(null);
    expect(unrecorded.tone).toBe("neutral");
  });

  it("describes experiment conclusions without spinning them", () => {
    expect(conclusionKindLabel("PARITY")).toEqual({ label: "Parity", tone: "neutral" });
    expect(conclusionKindLabel("OUTCOME_DIFFERENCES").label).toBe("Outcome differences");
    expect(conclusionKindLabel("NOT_INTERPRETABLE")).toEqual({
      label: "Not interpretable",
      tone: "warn",
    });
    expect(transitionDirectionLabel("COMPILED_LOSS").label).toBe("Raw succeeded, compiled did not");
    expect(transitionDirectionLabel("CHANGED").label).toBe("Changed failure mode");
  });

  it("names every trace event type the backend can emit", () => {
    for (const eventType of [
      "MODEL_REQUEST",
      "MODEL_RESPONSE",
      "TOOL_CALL",
      "TOOL_RESULT",
      "TOOL_ERROR",
      "AGENT_FINAL",
      "AGENT_ABORT",
      "PROVIDER_ERROR",
    ]) {
      expect(traceEventLabel(eventType)).not.toMatch(/_/);
    }
  });

  it("always says simulated for demand buckets", () => {
    expect(demandBucketLabel("captured")).toBe("Simulated captured demand");
    expect(demandBucketLabel("lost")).toMatch(/[Ss]imulated/);
    expect(demandBucketLabel("mystery")).toMatch(/[Ss]imulated/);
  });
});

describe("money formatting", () => {
  it("formats minor units with their currency code", () => {
    expect(formatMoney(499900, "INR")).toBe("INR\u00a04,999.00");
    expect(formatMoney(0, "USD")).toBe("USD\u00a00.00");
  });

  it("respects currencies without minor units instead of inventing decimals", () => {
    expect(formatMoney(1500, "JPY")).toBe("JPY\u00a01,500");
  });

  it("signs deltas explicitly", () => {
    expect(formatSignedMoney(-739100, "INR")).toBe("\u2212INR\u00a07,391.00");
    expect(formatSignedMoney(50000, "INR")).toBe("+INR\u00a0500.00");
    expect(formatSignedMoney(0, "INR")).toBe("INR\u00a00.00");
  });

  it("refuses floats, because money is never a float", () => {
    expect(() => formatMoney(10.5, "INR")).toThrow();
  });
});

describe("time and rate formatting", () => {
  it("renders timestamps in stated UTC", () => {
    expect(formatTimestamp("2026-08-23T10:04:30Z")).toMatch(/^23 Aug 2026.*UTC$/);
    expect(formatTimestamp(null)).toBe("not recorded");
  });

  it("reports an empty denominator rather than a fake percentage", () => {
    expect(formatRate(null)).toBe("no denominator");
    expect(formatRate(1)).toBe("100%");
    expect(formatRate(0.75)).toBe("75%");
    expect(formatRate(2 / 3)).toBe("66.7%");
  });
});
