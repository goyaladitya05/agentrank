import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ExperimentDetailContent } from "./ExperimentDetail";
import { outcomeDeltaRows, pairOrderLabel } from "@/lib/insights/experiment";
import { decodeRunMetrics } from "@/lib/insights/decode";
import { EXPERIMENT_DIFFERENCES_FIXTURE, EXPERIMENT_PARITY_FIXTURE } from "@/lib/insights/fixtures";
import type { ArmAggregate, RunMetrics } from "@/lib/insights/types";

function render(fixture: Record<string, unknown>): string {
  return renderToStaticMarkup(
    <ExperimentDetailContent data={decodeExperimentComparisonShim(fixture)} />,
  );
}

// The fixtures are wire shaped; decoding them keeps the render honest about the contract.
import { decodeExperimentComparison } from "@/lib/insights/decode";
function decodeExperimentComparisonShim(fixture: Record<string, unknown>) {
  return decodeExperimentComparison(fixture);
}

describe("<ExperimentDetailContent> parity", () => {
  const html = render(EXPERIMENT_PARITY_FIXTURE);

  it("renders the backend parity statement verbatim without spinning it", () => {
    expect(html).toContain("No measurable compiler benefit was observed at this sample size.");
    expect(html).toContain("Parity");
    const lowered = html.toLowerCase();
    expect(lowered).not.toContain("maintained");
    expect(lowered).not.toContain("100% performance");
    expect(lowered).not.toContain("compiler won");
  });

  it("puts methodology warnings above every number", () => {
    const warnings = html.indexOf("Methodology warnings");
    const outcomes = html.indexOf("Primary outcomes");
    expect(warnings).toBeGreaterThan(-1);
    expect(warnings).toBeLessThan(outcomes);
    expect(html).toContain("SMALL_SAMPLE");
    expect(html).toContain("cannot distinguish treatment effects from ordinary variation");
  });

  it("shows the sample count and designation beside the conclusion", () => {
    expect(html).toContain("2 completed of 2 declared");
    expect(html).toContain("Counterbalanced (odd pairs raw first, even pairs compiled first)");
    expect(html).toContain("Evaluation benchmark");
  });

  it("labels the arms by what each buyer could see", () => {
    expect(html).toContain("Raw (storefront view)");
    expect(html).toContain("Compiled (agent-ready view)");
  });

  it("explains that no transitions is what parity means", () => {
    expect(html).toContain("Every completed pair agreed on every mission");
  });
});

describe("<ExperimentDetailContent> differences", () => {
  const html = render(EXPERIMENT_DIFFERENCES_FIXTURE);

  it("names outcome differences and shows both development and small sample warnings", () => {
    expect(html).toContain("Outcome differences");
    expect(html).toContain("DEVELOPMENT_BENCHMARK");
    expect(html).toContain("SMALL_SAMPLE");
    expect(html).toContain("not independent evaluation evidence".replace(/^./, "n"));
  });

  it("shows the compiled loss transition with its failure reason", () => {
    expect(html).toContain("mission.travel.charger");
    expect(html).toContain("Raw succeeded, compiled did not");
    expect(html).toContain("STOCK_UNAVAILABLE");
  });

  it("signs the captured demand delta explicitly per currency", () => {
    expect(html).toContain("\u2212INR\u00a07,391.00");
    expect(html).toContain("+INR\u00a07,391.00");
  });
});

describe("outcomeDeltaRows", () => {
  const rawArm = (EXPERIMENT_DIFFERENCES_FIXTURE.arms as Record<string, unknown>[])[0];
  const compiledArm = (EXPERIMENT_DIFFERENCES_FIXTURE.arms as Record<string, unknown>[])[1];
  if (rawArm === undefined || compiledArm === undefined) {
    throw new Error("differences fixture is missing an arm");
  }
  const raw = decodeRunMetrics(rawArm["metrics_totals"]);
  const compiled = decodeRunMetrics(compiledArm["metrics_totals"]);

  it("never colors a safety regression as good news", () => {
    const worse = JSON.parse(JSON.stringify(compiled)) as RunMetrics;
    const baseline: RunMetrics = { ...raw, unsafe_completions: raw.unsafe_completions };
    const raised = { ...worse, unsafe_completions: raw.unsafe_completions + 2 };
    const rows = outcomeDeltaRows(baseline, raised);
    const escapeRow = rows.find((row) => row.metric === "Safety escapes");
    expect(escapeRow?.note).toMatch(/This is worse/);
    expect(escapeRow?.change).toBe("+2");
  });

  it("keeps ordinary rows neutral instead of editorializing", () => {
    const rows = outcomeDeltaRows(raw, compiled);
    for (const row of rows) {
      if (row.metric === "Purchase missions completed") {
        expect(row.note).toBeNull();
      }
    }
  });

  it("answers nothing when either arm has no comparable totals", () => {
    const arm = { metrics_totals: null } as unknown as ArmAggregate;
    void arm;
    expect(outcomeDeltaRows(null, compiled)).toEqual([]);
    expect(outcomeDeltaRows(raw, null)).toEqual([]);
  });
});

describe("pairOrderLabel", () => {
  it("says plainly when order was not counterbalanced", () => {
    expect(pairOrderLabel("raw_then_compiled")).toMatch(/Raw always ran first/);
    expect(pairOrderLabel("counterbalanced")).toMatch(/Counterbalanced/);
  });
});
