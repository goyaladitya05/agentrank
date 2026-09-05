import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { OverviewContent } from "./OverviewContent";
import { decodePreflight, type EvaluationPreflight } from "@/lib/evaluation";
import { decodeMerchantOverview } from "@/lib/insights/decode";
import { INITIAL_PREFLIGHT_FIXTURE, PREFLIGHT_FIXTURE } from "@/lib/evaluation-fixtures";
import { EXPERIMENT_PARITY_FIXTURE, OVERVIEW_FIXTURE, type Mutable } from "@/lib/insights/fixtures";
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

const REEVAL_PREFLIGHT = decodePreflight(PREFLIGHT_FIXTURE);
const INITIAL_PREFLIGHT = decodePreflight(INITIAL_PREFLIGHT_FIXTURE);

function render(
  overview: MerchantOverview,
  preflight: EvaluationPreflight | null = REEVAL_PREFLIGHT,
): string {
  return renderToStaticMarkup(<OverviewContent data={overview} preflight={preflight} />);
}

describe("the measured merchant's overview", () => {
  it("leads with how many purchase scenarios completed, from the API's own numbers", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("purchase scenarios completed");
    expect(html).toContain("<strong>8</strong>");
    expect(html).toContain("How well can AI agents shop from your store?");
    // The other counts stay on the page, behind the disclosure rather than beside the result.
    expect(html).toContain("Scenarios tested");
    expect(html).toContain("Successful purchases");
    expect(html).toContain("4 of 4");
    expect(html).toContain("Correct declines");
  });

  it("counts attention scenarios from findings, never from guesswork", () => {
    // The fixture's one merchant finding touches two distinct missions.
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("2 scenarios need your attention");
  });

  it("separates provider failures from merchant problems in the hero itself", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("Provider or system failures");
    expect(html).toContain("provider failures that need nothing from you");
  });

  it("lists merchant findings under attention and links each to its issue", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("What needs attention");
    expect(html).toContain('href="/issues/ATTRIBUTE_NOT_PUBLISHED%3Awattage"');
    expect(html).toContain("could not verify a required wattage attribute");
    // A finding with a proposed fix leads straight to it.
    expect(html).toContain('href="/fixes/01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1"');
    expect(html).toContain("Review fix");
  });

  it("never words a provider finding as merchant attention", () => {
    const html = render(
      overviewWith((data) => {
        data.top_findings = [
          data.top_findings[1] as NonNullable<(typeof data.top_findings)[number]>,
        ];
      }),
    );
    expect(html).toContain("Nothing needs your attention.");
    expect(html).not.toContain("You can fix this");
  });

  it("states that provider failures need no merchant action", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("1 mission ended on model provider failures");
    expect(html).toContain("No merchant action is required.");
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

  it("labels simulated demand as simulated and never implies actual revenue", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("Simulated potential");
    expect(html).toContain("Simulated captured");
    expect(html).toContain("Simulated lost");
    expect(html).toContain("Simulated demand captured");
    expect(html.toLowerCase()).not.toContain("gmv");
    expect(html).toContain("Not revenue.");
  });

  it("keeps currencies on separate rows instead of combining them", () => {
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("EUR 499.00");
    expect(html).toContain("INR 29,390.00");
    expect(html).toContain("one row per currency");
  });

  it("quotes the experiment conclusion verbatim and links into the Lab", () => {
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
    expect(html).toContain("/lab/experiments/");
    expect(html.toLowerCase()).not.toContain("100% performance");
  });
});

describe("the one next action", () => {
  it("leads a merchant with unreviewed facts to the fixes page with the count", () => {
    // The fixture's representation state carries three facts awaiting review.
    const html = render(overviewWith(() => undefined));
    expect(html).toContain("Review 3 fixes");
    expect(html).toContain('href="/fixes"');
  });

  it("leads a merchant whose fixes are reviewed and published to measure again", () => {
    const html = render(
      overviewWith((data) => {
        repState(data).review_required_facts = 0;
        data.top_findings = [];
      }),
    );
    expect(html).toContain("Measure again");
    expect(html).toContain('href="/evaluations"');
  });

  it("leads a merchant with merchant-fixable findings and no facts to the issues", () => {
    const html = render(
      overviewWith((data) => {
        repState(data).review_required_facts = 0;
      }),
      null,
    );
    expect(html).toContain("Review issues");
    expect(html).toContain('href="/issues"');
  });

  it("follows a pending evaluation above everything else", () => {
    const pending = {
      ...REEVAL_PREFLIGHT,
      pending_launch_id: "01a00000-0000-7000-8000-00000000aaaa",
    };
    const html = render(
      overviewWith(() => undefined),
      pending,
    );
    expect(html).toContain("Follow the evaluation");
    expect(html).toContain("/evaluations/01a00000-0000-7000-8000-00000000aaaa");
  });
});

describe("a merchant with nothing measured yet", () => {
  function unmeasured(mutate?: (overview: Mutable<MerchantOverview>) => void): MerchantOverview {
    return overviewWith((data) => {
      data.top_findings = [];
      data.top_findings_run_id = null;
      data.runs = [];
      data.simulated_demand_totals_by_currency = [];
      mutate?.(data);
    });
  }

  it("walks the journey and leads to the first evaluation when one can run", () => {
    const html = render(unmeasured(), INITIAL_PREFLIGHT);
    expect(html).toContain("Can AI shopping agents <em>buy</em> from your store?");
    expect(html).toContain("You are here");
    expect(html).toContain("Run your first evaluation");
    expect(html).toContain('href="/evaluations"');
    // No invented numbers in place of the measurement nobody has taken.
    expect(html).not.toContain("0%");
    expect(html).not.toContain("Simulated captured");
  });

  it("leads a merchant with no source at all to the import", () => {
    const html = render(
      unmeasured((data) => {
        repState(data).source_snapshot_id = null;
        repState(data).source_snapshot_label = null;
        repState(data).compiled_representation_id = null;
        repState(data).compiled_representation_label = null;
        repState(data).review_required_facts = 0;
      }),
      null,
    );
    expect(html).toContain("Import your store");
    expect(html).toContain('href="/sources/import"');
    expect(html).toContain("Nothing about your live store changes");
  });
});

describe("an evaluation that stopped part way", () => {
  it("says the run is incomplete beside its own numbers", () => {
    const html = render(
      overviewWith((overview) => {
        const [latest] = overview.runs;
        if (latest === undefined) throw new Error("the fixture has no runs");
        latest.status = "ABORTED";
        overview.top_findings = [];
      }),
    );
    expect(html).toContain("stopped before it finished");
    expect(html).toContain("describe only the scenarios that executed");
  });

  it("does not claim an all-clear from a partial run", () => {
    const html = render(
      overviewWith((overview) => {
        const [latest] = overview.runs;
        if (latest === undefined) throw new Error("the fixture has no runs");
        latest.status = "ABORTED";
        overview.top_findings = [];
      }),
    );
    expect(html).toContain(
      "No merchant-fixable finding came out of the part of this evaluation that executed.",
    );
    expect(html).not.toContain("Your latest evaluation produced no finding");
  });
});
