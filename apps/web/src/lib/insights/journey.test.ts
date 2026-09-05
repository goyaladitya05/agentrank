import { describe, expect, it } from "vitest";

import { scenarioJourney, scenarioName, stagesReached } from "@/lib/insights/journey";
import type { MissionDiagnosis, MissionFinding } from "@/lib/insights/types";

function finding(overrides: Partial<MissionFinding> = {}): MissionFinding {
  return {
    code: "ATTRIBUTE_NOT_PUBLISHED",
    owner: "MERCHANT_CATALOG",
    actionability: "MERCHANT_ACTION",
    severity: "MEDIUM",
    evidence_level: "TRUSTED_FACT",
    summary: "A required attribute is not published.",
    recommendation: null,
    attribute_keys: [],
    product_ids: [],
    variant_ids: [],
    evidence: [],
    compiler_references: [],
    ...overrides,
  };
}

function mission(overrides: Partial<MissionDiagnosis> = {}): MissionDiagnosis {
  return {
    engine_identity: "sha256:engine",
    run_id: "01990000-0000-7000-8000-000000000001",
    mission_run_id: "01990000-0000-7000-8000-000000000002",
    mission_key: "mission.one",
    status: "FAILED",
    outcome: "did not complete",
    primary_code: "ATTRIBUTE_NOT_PUBLISHED",
    findings: [finding()],
    simulated_demand: [],
    model_invocations: null,
    tool_calls: null,
    tool_errors: null,
    ...overrides,
  };
}

describe("scenarioJourney", () => {
  it("takes a completed purchase all the way to checkout", () => {
    const journey = scenarioJourney(mission({ status: "SUCCEEDED", primary_code: null }));
    expect(journey.outcome).toBe("completed");
    expect(journey.reached).toBe("CHECKOUT");
    expect(journey.stoppedBecause).toBeNull();
    expect(stagesReached(journey)).toBe(4);
  });

  it("stops a missing-attribute failure at understand and owns it to the merchant", () => {
    const journey = scenarioJourney(mission());
    expect(journey.outcome).toBe("blocked");
    expect(journey.reached).toBe("UNDERSTAND");
    expect(journey.stoppedBecause).toContain("not published");
  });

  it("stops a discovery failure at discover", () => {
    const journey = scenarioJourney(
      mission({
        primary_code: "DISCOVERY_FAILED",
        findings: [finding({ code: "DISCOVERY_FAILED", summary: "Nothing was found to buy." })],
      }),
    );
    expect(journey.reached).toBe("DISCOVER");
  });

  it("believes cited commerce artifacts over the diagnosis code", () => {
    // A payment attempt is the commerce kernel's own record that the attempt got that far,
    // whatever the code says about why it then failed.
    const journey = scenarioJourney(
      mission({
        findings: [
          finding({ evidence: [{ kind: "payment_attempt", identifier: "p1", establishes: "x" }] }),
        ],
      }),
    );
    expect(journey.reached).toBe("CHECKOUT");
  });

  it("reads a correct decline as intentional rather than as a failure", () => {
    const journey = scenarioJourney(
      mission({ status: "ABSTAINED", primary_code: null, findings: [] }),
    );
    expect(journey.outcome).toBe("declined");
    expect(journey.stoppedBecause).toContain("Declined correctly");
  });

  it("separates a provider interruption from anything the merchant owns", () => {
    const journey = scenarioJourney(
      mission({
        primary_code: "PROVIDER_OUTAGE_TERMINATED_MISSION",
        findings: [
          finding({
            code: "PROVIDER_OUTAGE_TERMINATED_MISSION",
            owner: "MODEL_PROVIDER",
            actionability: "NO_MERCHANT_ACTION",
            summary: "The model provider never produced a usable response.",
          }),
        ],
      }),
    );
    expect(journey.outcome).toBe("interrupted");
  });
});

describe("scenarioName", () => {
  it("spells a mission key the way a merchant reads it", () => {
    expect(scenarioName("black-100w-charger")).toBe("Black 100w charger");
    expect(scenarioName("mission.usb-c.charger")).toBe("Usb c charger");
  });

  it("leaves a key it cannot read as it is", () => {
    expect(scenarioName("")).toBe("");
  });
});
