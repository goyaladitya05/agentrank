import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LaunchAccepted, LaunchConfirmation } from "./LaunchEvaluation";
import { EvaluationLaunchDetailContent } from "./EvaluationLaunchDetail";
import { decodePreflight, decodeEvaluationLaunchDetail } from "@/lib/evaluation";
import { IDLE_LAUNCH, type LaunchState } from "@/lib/evaluation-mutation";
import {
  COMPARISON_FIXTURE,
  COMPLETED_INITIAL_FIXTURE,
  COMPLETED_REEVALUATION_FIXTURE,
  FAILED_REEVALUATION_FIXTURE,
  INCOMPARABLE_REEVALUATION_FIXTURE,
  INITIAL_BLOCKED_PREFLIGHT_FIXTURE,
  INITIAL_PREFLIGHT_FIXTURE,
  PREFLIGHT_FIXTURE,
  QUEUED_INITIAL_FIXTURE,
  QUEUED_REEVALUATION_FIXTURE,
  REFERENCE_PREFLIGHT_FIXTURE,
  RUNNING_REEVALUATION_FIXTURE,
  SURFACE_CHANGE_PREFLIGHT_FIXTURE,
} from "@/lib/evaluation-fixtures";

function render(fixture: Record<string, unknown>): string {
  return renderToStaticMarkup(
    <EvaluationLaunchDetailContent launch={decodeEvaluationLaunchDetail(fixture)} />,
  );
}

function confirmation(
  fixture: Record<string, unknown>,
  state: LaunchState = IDLE_LAUNCH,
  pending = false,
): string {
  return renderToStaticMarkup(
    <LaunchConfirmation
      preflight={decodePreflight(fixture)}
      action={() => undefined}
      state={state}
      pending={pending}
    />,
  );
}

describe("<LaunchConfirmation>", () => {
  it("says what will run, what it costs in execution and what it does not touch", () => {
    const html = confirmation(PREFLIGHT_FIXTURE);
    expect(html).toContain("voltedge-core@2");
    expect(html).toContain("14 missions are executed");
    expect(html).toContain("Every previous run and its findings stay exactly as they are");
    expect(html).toContain("can consume quota or incur cost");
    expect(html).toContain("12 model turns per mission");
    expect(html).toContain("24 tool calls per mission");
  });

  it("never states a currency amount for the run itself", () => {
    const html = confirmation(PREFLIGHT_FIXTURE);
    expect(html).not.toMatch(/estimated cost/i);
    expect(html).not.toMatch(/\$\d/);
    expect(html).not.toMatch(/costs? (about|around|approximately)/i);
  });

  it("says plainly when the buyer is not an AI agent", () => {
    const html = confirmation(REFERENCE_PREFLIGHT_FIXTURE);
    expect(html).toContain("No AI model provider is configured");
    expect(html).toContain("It is not an AI agent");
    expect(html).toContain("reads neither your storefront nor any published representation");
    expect(html).toContain("nothing to compare against yet");
    // A buyer with no model has no model bounds, and none are invented for it.
    expect(html).not.toContain("model turns per mission");
  });

  it("says before spending when the earlier run measured a different kind of surface", () => {
    const html = confirmation(SURFACE_CHANGE_PREFLIGHT_FIXTURE);
    expect(html).toContain("measured a different kind of surface");
    expect(html).toContain("what a controlled experiment is for");
    expect(html).not.toContain("will be shown beside your most recent completed run");
  });

  it("disables submission while a request is in flight", () => {
    const html = confirmation(PREFLIGHT_FIXTURE, IDLE_LAUNCH, true);
    expect(html).toContain("disabled");
    expect(html).toContain("Requesting the evaluation");
  });

  it("distinguishes a refused launch from a response it never saw", () => {
    const refused = confirmation(PREFLIGHT_FIXTURE, {
      ok: false,
      message: "A re-evaluation is already queued or running for your merchant.",
      stale: true,
      unknown: false,
      launchId: null,
    });
    expect(refused).toContain("already queued or running");
    expect(refused).toContain("The state shown here is current.");

    const lost = confirmation(PREFLIGHT_FIXTURE, {
      ok: false,
      message:
        "The console could not reach AgentRank, so whether this launch was accepted is unknown.",
      stale: false,
      unknown: true,
      launchId: null,
    });
    expect(lost).toContain("is unknown");
    expect(lost).not.toContain("The state shown here is current.");
  });
});

describe("<LaunchAccepted>", () => {
  it("acknowledges an accepted launch and links to it", () => {
    const html = renderToStaticMarkup(
      <LaunchAccepted
        state={{
          ok: true,
          message: null,
          stale: false,
          unknown: false,
          launchId: "01a0cccc-cccc-7ccc-8ccc-ccccccccccc1",
        }}
      />,
    );
    expect(html).toContain("Evaluation requested");
    expect(html).toContain("Nothing has been executed yet");
    expect(html).toContain("/evaluations/01a0cccc-cccc-7ccc-8ccc-ccccccccccc1");
  });
});

describe("<EvaluationLaunchDetailContent>", () => {
  it("says a queued launch has executed nothing", () => {
    const html = render(QUEUED_REEVALUATION_FIXTURE);
    expect(html).toContain("Nothing has been executed yet");
    expect(html).toContain("no model quota has been spent");
    expect(html).not.toContain("Open the benchmark run");
  });

  it("reports real mission counts and never a percentage", () => {
    const html = render(RUNNING_REEVALUATION_FIXTURE);
    expect(html).toContain("7 of 14 missions finished");
    expect(html).not.toMatch(/\d+%/);
    expect(html).toContain("re-reads the launch every 10 seconds");
    expect(html).toContain("Open the benchmark run");
  });

  it("explains a launch that could not run without blaming the merchant", () => {
    const html = render(FAILED_REEVALUATION_FIXTURE);
    expect(html).toContain("no credential for the model provider");
    expect(html).toContain("No benchmark run was started");
    expect(html).toContain("no previous evidence changed");
    expect(html).not.toContain("provider_credential_unavailable");
  });

  it("leads a completed comparison with its caveats", () => {
    const html = render(COMPLETED_REEVALUATION_FIXTURE);
    expect(html).toContain("14 of 14 missions finished");
    expect(html).toContain("Not a controlled experiment");
    expect(html).toContain("One run on each side");
    expect(html).toContain("Newly completed");
    expect(html).toContain("Token usage unknown");
    expect(html).toContain("token totals are unknown");
  });

  it("keeps simulated demand per currency and labelled simulated", () => {
    const html = render(COMPLETED_REEVALUATION_FIXTURE);
    expect(html).toContain("Simulated before");
    expect(html).toContain("Simulated after");
    expect(html).toContain("currencies are never added together");
    expect(html).toContain("EUR");
    expect(html).toContain("INR");
  });

  it("publishes no weighted score anywhere", () => {
    const html = render(COMPLETED_REEVALUATION_FIXTURE);
    expect(html).not.toMatch(/agentrank score/i);
    expect(html).not.toMatch(/overall score/i);
  });

  it("says which side recorded a model trace rather than claiming neither did", () => {
    const oneSided = {
      ...COMPLETED_REEVALUATION_FIXTURE,
      comparison: {
        ...COMPARISON_FIXTURE,
        // A side with no provider invocation reported nothing about tokens, so the API raises
        // no token warning for it either.
        warnings: COMPARISON_FIXTURE.warnings.filter(
          (warning) => warning.code !== "TOKEN_USAGE_UNAVAILABLE",
        ),
        interactions: {
          model_invocations: null,
          tool_calls: null,
          baseline_traced: false,
          candidate_traced: true,
          token_usage_complete: null,
        },
      },
    };
    const html = renderToStaticMarkup(
      <EvaluationLaunchDetailContent launch={decodeEvaluationLaunchDetail(oneSided)} />,
    );
    expect(html).toContain("Only the later run recorded a model trace");
    expect(html).not.toContain("Neither run recorded a model trace");
    // No provider invocation reported nothing, so nothing claims one did.
    expect(html).not.toContain("reported no token usage");
  });

  it("refuses to draw a comparison from two runs that measured different things", () => {
    const html = render(INCOMPARABLE_REEVALUATION_FIXTURE);
    expect(html).toContain("did not measure the same thing");
    expect(html).toContain("Different workload");
    // No deltas are shown beside a refusal to compare.
    expect(html).not.toContain("Compliant purchases");
  });

  it("says why there is nothing to compare rather than showing an empty comparison", () => {
    const html = render({
      ...COMPLETED_REEVALUATION_FIXTURE,
      baseline_run_id: null,
      comparison: null,
    });
    expect(html).toContain("no earlier completed run of this suite");
    expect(html).not.toContain("Parity");
  });

  it("offers the compiler facts behind the representation it tested", () => {
    const html = render(COMPLETED_REEVALUATION_FIXTURE);
    expect(html).toContain("/compiler/runs/01a0aaaa-aaaa-7aaa-8aaa-aaaaaaaaaaa1");
  });
});

describe("a merchant's first evaluation", () => {
  it("says what it measures without naming an artifact nobody has published", () => {
    const html = confirmation(INITIAL_PREFLIGHT_FIXTURE);
    expect(html).toContain("against your merchant as it is now");
    expect(html).toContain("voltedge-source@1");
    expect(html).toContain("The buyer reads the ordinary storefront");
    expect(html).toContain("This creates your first benchmark result");
    expect(html).toContain("Request first evaluation");
    expect(html).not.toContain("representation compiler:");
  });

  it("states the absence of a before rather than a zero", () => {
    const html = confirmation(INITIAL_PREFLIGHT_FIXTURE);
    expect(html).toContain("You have no earlier result");
    expect(html).toContain("does not report a change where there is nothing to change from");
    expect(html).not.toContain("0%");
    expect(html).not.toContain("No change");
    expect(html).not.toContain("Baseline score");
  });

  it("says a run does not move merchant money or stock", () => {
    expect(confirmation(INITIAL_PREFLIGHT_FIXTURE)).toContain(
      "does not change your prices, inventory or any payment",
    );
  });

  it("names no currency amount for the run itself", () => {
    const html = confirmation(INITIAL_PREFLIGHT_FIXTURE);
    expect(html).not.toMatch(/[$£€]\s?\d/);
    expect(html).toContain("AgentRank does not estimate that amount");
  });

  it("carries a blocker a merchant can clear themselves", () => {
    const preflight = decodePreflight(INITIAL_BLOCKED_PREFLIGHT_FIXTURE);
    expect(preflight.launchable).toBe(false);
    expect(preflight.blockers[0]?.code).toBe("merchant_source_unavailable");
    expect(preflight.source_snapshot_id).toBeNull();
  });

  it("renders a queued first evaluation with no comparison section at all", () => {
    const html = render(QUEUED_INITIAL_FIXTURE);
    expect(html).toContain("First evaluation");
    expect(html).toContain("Nothing has been executed yet");
    expect(html).not.toContain("Compared with your previous run");
    expect(html).not.toContain("No comparison yet");
  });

  it("names the merchant state it measured rather than a representation", () => {
    const html = render(COMPLETED_INITIAL_FIXTURE);
    expect(html).toContain("through the ordinary storefront");
    expect(html).toContain("voltedge-source@1");
    expect(html).toContain("14 of 14 missions finished");
  });

  it("offers ordinary product navigation and never a compiler run it did not have", () => {
    const html = render(COMPLETED_INITIAL_FIXTURE);
    expect(html).toContain("Review your merchant source");
    expect(html).toContain('href="/sources"');
    expect(html).not.toContain("/compiler/runs/");
    expect(html).not.toContain("Review the compiler facts");
  });

  it("reaches the ordinary run surfaces once it completes", () => {
    const html = render(COMPLETED_INITIAL_FIXTURE);
    expect(html).toContain("Open the benchmark run and its findings");
    expect(html).toContain("/runs/01a0dddd-dddd-7ddd-8ddd-ddddddddddd2");
  });
});
