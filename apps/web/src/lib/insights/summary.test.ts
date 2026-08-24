import { describe, expect, it } from "vitest";

import { attentionSentences, attentionSummary, providerSentence, safetyReading } from "./summary";
import type { MerchantFinding } from "./types";

function finding(actionability: MerchantFinding["actionability"]): MerchantFinding {
  return {
    key: `k-${actionability}`,
    code: "CODE",
    owner: "MERCHANT_CATALOG",
    actionability,
    severity: "MEDIUM",
    evidence_level: "TRUSTED_FACT",
    title: "t",
    recommendation: null,
    mission_run_ids: ["m1"],
    mission_keys: ["mission"],
    product_ids: [],
    variant_ids: [],
    attribute_keys: [],
    simulated_demand: [],
  };
}

describe("attention summary", () => {
  it("keeps the four audiences apart", () => {
    const summary = attentionSummary([
      finding("MERCHANT_ACTION"),
      finding("MERCHANT_ACTION"),
      finding("REVIEW_REQUIRED"),
      finding("NO_MERCHANT_ACTION"),
      finding("AGENT_SYSTEM_ACTION"),
    ]);
    expect(summary.totalFindings).toBe(5);
    expect(summary.merchantAction).toBe(2);
    expect(summary.reviewRequired).toBe(1);
    expect(summary.systemOrExternal).toBe(1);
    expect(summary.agentSystemAction).toBe(1);
  });

  it("answers an all clear with no findings", () => {
    const summary = attentionSummary([]);
    expect(summary.totalFindings).toBe(0);
    expect(attentionSentences(summary)).toEqual([]);
  });

  it("directs the merchant to their findings first when they have any", () => {
    const sentences = attentionSentences(attentionSummary([finding("MERCHANT_ACTION")]));
    expect(sentences.at(-1)).toMatch(/Start with the findings marked as yours/);
  });

  it("never asks a merchant to act on external or informational findings", () => {
    const sentences = attentionSentences(
      attentionSummary([finding("NO_MERCHANT_ACTION"), finding("AGENT_SYSTEM_ACTION")]),
    );
    expect(sentences.at(-1)).toBe("No finding requires action from you.");
    expect(sentences[0]).toContain("external or informational");
    expect(sentences[0]).toContain("AgentRank system work");
  });
});

describe("safety reading", () => {
  it("treats an escape as the most serious reading and never as enforcement working", () => {
    const escape = safetyReading(3, 1, 0);
    expect(escape?.tone).toBe("fail");
    expect(escape?.text).toMatch(/[Pp]urchases completed past a refusal/);
  });

  it("reports blocked attempts as evidence safety worked", () => {
    const blocked = safetyReading(2, 0, 0);
    expect(blocked?.tone).toBe("ok");
    expect(blocked?.text).toMatch(/all blocked before money moved/);
  });

  it("distinguishes unverifiable attempts from escapes", () => {
    const unverified = safetyReading(0, 0, 2);
    expect(unverified?.tone).toBe("warn");
    expect(unverified?.text).toMatch(/could not be verified/);
  });

  it("answers nothing to report with null rather than a fake clean bill", () => {
    expect(safetyReading(0, 0, 0)).toBeNull();
  });
});

describe("provider sentence", () => {
  it("states explicitly that outages need no merchant action", () => {
    expect(providerSentence(3)).toMatch(/No merchant action is required/);
    expect(providerSentence(1)).toMatch(/^1 mission ended/);
    expect(providerSentence(0)).toBeNull();
  });
});
