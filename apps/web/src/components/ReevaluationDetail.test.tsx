import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LaunchConfirmation } from "./LaunchReevaluation";
import { ReevaluationDetailContent } from "./ReevaluationDetail";
import { decodePreflight, decodeReevaluationDetail } from "@/lib/reevaluation";
import { IDLE_LAUNCH, type LaunchState } from "@/lib/reevaluation-mutation";
import {
  COMPLETED_REEVALUATION_FIXTURE,
  FAILED_REEVALUATION_FIXTURE,
  INCOMPARABLE_REEVALUATION_FIXTURE,
  PREFLIGHT_FIXTURE,
  QUEUED_REEVALUATION_FIXTURE,
  REFERENCE_PREFLIGHT_FIXTURE,
  RUNNING_REEVALUATION_FIXTURE,
} from "@/lib/reevaluation-fixtures";

function render(fixture: Record<string, unknown>): string {
  return renderToStaticMarkup(
    <ReevaluationDetailContent launch={decodeReevaluationDetail(fixture)} />,
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
    expect(html).toContain("does not read your published representation");
    expect(html).toContain("nothing to compare against yet");
    // A buyer with no model has no model bounds, and none are invented for it.
    expect(html).not.toContain("model turns per mission");
  });

  it("disables submission while a request is in flight", () => {
    const html = confirmation(PREFLIGHT_FIXTURE, IDLE_LAUNCH, true);
    expect(html).toContain("disabled");
    expect(html).toContain("Requesting the re-evaluation");
  });

  it("distinguishes a refused launch from a response it never saw", () => {
    const refused = confirmation(PREFLIGHT_FIXTURE, {
      ok: false,
      message: "A re-evaluation is already queued or running for your merchant.",
      stale: true,
      unknown: false,
      reevaluationId: null,
    });
    expect(refused).toContain("already queued or running");
    expect(refused).toContain("The state shown here is current.");

    const lost = confirmation(PREFLIGHT_FIXTURE, {
      ok: false,
      message:
        "The console could not reach AgentRank, so whether this launch was accepted is unknown.",
      stale: false,
      unknown: true,
      reevaluationId: null,
    });
    expect(lost).toContain("is unknown");
    expect(lost).not.toContain("The state shown here is current.");
  });
});

describe("<ReevaluationDetailContent>", () => {
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
