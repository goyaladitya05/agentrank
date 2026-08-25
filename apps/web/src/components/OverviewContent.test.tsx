import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OverviewContent } from "./OverviewContent";
import { decodeMerchantOverview } from "@/lib/insights/decode";
import { EXPERIMENT_PARITY_FIXTURE, OVERVIEW_FIXTURE, type Mutable } from "@/lib/insights/fixtures";
import type { MerchantOverview } from "@/lib/insights/types";

function overviewWith(mutate: (overview: Mutable<MerchantOverview>) => void): MerchantOverview {
  const clone = structuredClone(OVERVIEW_FIXTURE) as unknown as MerchantOverview;
  mutate(clone as Mutable<MerchantOverview>);
  return decodeMerchantOverview(clone);
}

function render(overview: MerchantOverview): string {
  return renderToStaticMarkup(<OverviewContent data={overview} />);
}

describe("<OverviewContent> product behavior", () => {
  it("renders the latest run's real numbers from the API", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("8 of 14 purchase missions succeeded");
    expect(html).toContain("INR\u00a021,000.00");
    expect(html).toContain("voltedge-core@2");
  });

  it("marks a development benchmark run wherever its numbers appear", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("Development benchmark");
    expect(html).toContain("Not independent evaluation evidence.");
  });

  it("answers whether anything needs attention before every metric", () => {
    const html = render(overviewWith(() => undefined));
    const attention = html.indexOf("Do I need to do something?");
    const health = html.indexOf("Latest benchmark health");
    const findings = html.indexOf("Top findings");
    expect(attention).toBeGreaterThan(-1);
    expect(attention).toBeLessThan(health);
    expect(health).toBeLessThan(findings);
  });

  it("splits merchant work from provider issues in the attention summary", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("1 needs your action");
    expect(html).toContain("1 is external or informational");
    expect(html).toContain("Start with the findings marked as yours");
  });

  it("never words a provider finding as merchant work", () => {
    const html = render(
      overviewWith((data) => {
        data.top_findings = [
          data.top_findings[1] as NonNullable<(typeof data.top_findings)[number]>,
        ];
        data.runs = [];
        data.top_findings_run_id = null;
      }),
    );
    expect(html).toContain("No action required from you");
    expect(html).toContain("Model provider");
    expect(html).not.toContain("You can fix this");
  });

  it("shows an all clear when a completed run produced no findings", () => {
    const html = render(
      overviewWith((data) => {
        data.top_findings = [];
        data.runs = [structuredClone(data.runs[0] as NonNullable<typeof data.runs>[number])];
      }),
    );
    expect(html).toContain("No findings on this run. Nothing here needs your action.");
  });

  it("renders an empty state when no run has finished yet", () => {
    const html = render(
      overviewWith((data) => {
        data.top_findings = [];
        data.top_findings_run_id = null;
        data.runs = [];
        data.simulated_demand_totals_by_currency = [];
      }),
    );
    expect(html).toContain("No evaluations have run yet");
    expect(html).toContain("No findings");
    expect(html).toContain("No runs yet");
  });

  it("offers a merchant with nothing measured the one action that changes that", () => {
    const html = render(
      overviewWith((data) => {
        data.top_findings = [];
        data.top_findings_run_id = null;
        data.runs = [];
        data.simulated_demand_totals_by_currency = [];
      }),
    );
    expect(html).toContain('href="/evaluations"');
    expect(html).toContain("Run your first evaluation");
    // No operator instruction where a merchant now has a button, and no invented numbers in
    // place of the measurement nobody has taken.
    expect(html).not.toContain("benchmark command line");
    expect(html).not.toContain("0%");
  });

  it("labels simulated demand as simulated and never implies actual revenue", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("Simulated potential");
    expect(html).toContain("Simulated captured");
    expect(html).toContain("Simulated lost");
    expect(html.toLowerCase()).not.toContain("gmv");
    expect(html).toContain("Benchmark figures. Not revenue.");
  });

  it("keeps currencies on separate rows instead of combining them", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("EUR\u00a0499.00");
    expect(html).toContain("INR\u00a029,390.00");
    // The table states its grouping rule where a combined total would otherwise be assumed.
    expect(html).toContain("one row per currency");
  });

  it("reports blocked unsafe attempts as safety working, not as failure", () => {
    const html = render(
      overviewWith((data) => {
        const run = data.runs[0] as NonNullable<(typeof data.runs)[number]>;
        run.unsafe_attempts = 2;
        run.unsafe_completions = 0;
      }),
    );
    expect(html).toContain("2 unsafe purchase attempts, all blocked before money moved.");
  });

  it("treats a safety escape as the most serious safety reading", () => {
    const html = render(
      overviewWith((data) => {
        const run = data.runs[0] as NonNullable<(typeof data.runs)[number]>;
        run.unsafe_attempts = 2;
        run.unsafe_completions = 1;
      }),
    );
    expect(html).toContain("1 safety escape recorded");
    expect(html).toContain("Purchases completed past a refusal");
  });

  it("states that provider failures need no merchant action", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("1 mission ended on model provider failures");
    expect(html).toContain("No merchant action is required.");
  });

  it("quotes the experiment conclusion verbatim and links to the comparison", () => {
    const html = render(
      overviewWith((data) => {
        if (data.latest_experiment !== null) {
          data.latest_experiment.conclusion_statement =
            EXPERIMENT_PARITY_FIXTURE.conclusion.statement;
        }
      }),
    );
    expect(html).toContain("No measurable compiler benefit was observed at this sample size.");
    expect(html).toContain("Parity");
    expect(html).toContain("/experiments/");
    expect(html.toLowerCase()).not.toContain("100% performance");
    expect(html.toLowerCase()).not.toContain("compiler maintained");
  });

  it("surfaces the small sample warning beside the conclusion, not below the fold", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("SMALL_SAMPLE");
    expect(html).toContain("cannot distinguish treatment effects from ordinary variation");
  });

  it("shows representation review state without compiler internals", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("3 fact(s) awaiting merchant review");
    expect(html).toContain("VoltEdge agent-ready IR");
    expect(html).toContain('href="/compiler"');
    expect(html).toContain("Review 3 semantic fact(s)");
    expect(html).not.toContain("provenance");
    expect(html).not.toContain("confidence");
  });

  it("reports an empty denominator honestly instead of a percentage", () => {
    const html = render(
      overviewWith((data) => {
        const run = data.runs[0] as NonNullable<(typeof data.runs)[number]>;
        run.task_completion_rate = null;
      }),
    );
    expect(html).toContain("no denominator");
  });

  it("points a published representation at the command that measures it", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("Publishing a representation never runs a benchmark");
    expect(html).toContain('href="/evaluations"');
  });
});
