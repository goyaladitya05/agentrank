import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CandidateReviewForm } from "./CandidateReview";
import { decodeCompilerRun, type CompilerCandidate } from "@/lib/compiler";
import {
  COMPILER_RUN_FIXTURE,
  CORRECTION_REVIEW_FIXTURE,
  REJECT_REVIEW_FIXTURE,
} from "@/lib/compiler-fixtures";
import { IDLE_MUTATION, type CompilerMutationState } from "@/lib/compiler-mutation";

const RUN = decodeCompilerRun(COMPILER_RUN_FIXTURE);
const NEEDS_CORRECTION = RUN.candidates[0] as CompilerCandidate;
const NEEDS_DECISION = RUN.candidates[1] as CompilerCandidate;
const COMPILER_ACCEPTED = RUN.candidates[2] as CompilerCandidate;

function render(
  candidate: CompilerCandidate,
  state: CompilerMutationState = IDLE_MUTATION,
  pending = false,
): string {
  return renderToStaticMarkup(
    <CandidateReviewForm
      candidate={candidate}
      action="/compiler"
      state={state}
      pending={pending}
    />,
  );
}

function withReview(candidate: CompilerCandidate, review: unknown): CompilerCandidate {
  const clone = structuredClone(COMPILER_RUN_FIXTURE) as typeof COMPILER_RUN_FIXTURE;
  const target = clone.candidates.find((item) => item.candidate_id === candidate.candidate_id);
  if (target === undefined) throw new Error("fixture candidate not found");
  Object.assign(target, { review });
  const decoded = decodeCompilerRun(clone).candidates.find(
    (item) => item.candidate_id === candidate.candidate_id,
  );
  if (decoded === undefined) throw new Error("decoded candidate not found");
  return decoded;
}

describe("<CandidateReviewForm> review queue behavior", () => {
  it("offers accept and reject for a fact the compiler read but cannot confirm", () => {
    const html = render(NEEDS_DECISION);
    expect(html).toContain('value="accept"');
    expect(html).toContain('value="reject"');
    expect(html).toContain("Accept fact");
    expect(html).toContain("Reject fact");
  });

  it("refuses to offer accept for a fact the source contradicts, and says why", () => {
    const html = render(NEEDS_CORRECTION);
    expect(html).not.toContain('value="accept"');
    expect(html).toContain("Your source states more than one value");
    expect(html).toContain("Confirm correction");
  });

  it("asks a compatibility correction as a four state choice, never free JSON", () => {
    const html = render(NEEDS_DECISION);
    expect(html).toContain("Yes, it is compatible");
    expect(html).toContain("Not known");
    expect(html).toContain("Does not apply");
    expect(html).not.toContain("attribute_kind");
  });

  it("asks a measurement correction as a number and cites the source field it came from", () => {
    const html = render(NEEDS_CORRECTION);
    expect(html).toContain('type="number"');
    expect(html).toContain('value="products[VE-CHG-100].title"');
    expect(html).toContain('value="100W"');
  });

  it("shows nothing to decide for a fact the compiler accepted on its own", () => {
    const html = render(COMPILER_ACCEPTED);
    expect(html).toContain("Accepted by the compiler");
    expect(html).not.toContain("<form");
    expect(html).not.toContain("<button");
  });
});

describe("<CandidateReviewForm> after a decision", () => {
  it("reports a correction as the merchant's, and says the proposal is kept", () => {
    const html = render(withReview(NEEDS_CORRECTION, CORRECTION_REVIEW_FIXTURE));
    expect(html).toContain("Corrected by you");
    expect(html).toContain("Corrected to 65 W");
    expect(html).toContain("The compiler proposal is kept.");
    expect(html).not.toContain("Confirm correction");
  });

  it("reports a rejection and offers no way to review the same fact again", () => {
    const html = render(withReview(NEEDS_DECISION, REJECT_REVIEW_FIXTURE));
    expect(html).toContain("Rejected by you");
    expect(html).not.toContain("<form");
  });
});

describe("<CandidateReviewForm> when a write fails", () => {
  const conflict: CompilerMutationState = {
    ok: false,
    message: "Someone already reviewed this fact. The decision now shown is the one that counts.",
    stale: true,
    values: null,
  };
  const invalid: CompilerMutationState = {
    ok: false,
    message: "measurement correction value is not supported by cited source evidence",
    stale: false,
    values: {
      value: "999",
      provenanceField: "products[VE-CHG-100].description",
      provenanceExcerpt: "65W",
    },
  };

  it("states a conflict inline and says the state beside it is current", () => {
    const html = render(NEEDS_DECISION, conflict);
    expect(html).toContain('role="alert"');
    expect(html).toContain("Someone already reviewed this fact");
    expect(html).toContain("The state shown here is current.");
  });

  it("keeps everything the merchant typed when the API refuses the correction", () => {
    const html = render(NEEDS_CORRECTION, invalid);
    expect(html).toContain('value="999"');
    expect(html).toContain('value="products[VE-CHG-100].description"');
    expect(html).toContain('value="65W"');
    expect(html).toContain("not supported by cited source evidence");
  });

  it("reopens the correction fields when a correction failed on an acceptable fact", () => {
    expect(render(NEEDS_DECISION, invalid)).toMatch(/<details[^>]*open=""/);
    expect(render(NEEDS_DECISION)).not.toMatch(/<details[^>]*open=""/);
  });

  it("shows the merchant that a decision is in flight and blocks a second submit", () => {
    const html = render(NEEDS_DECISION, IDLE_MUTATION, true);
    expect(html).toContain('role="status"');
    expect(html).toContain("Saving your decision");
    expect(html.match(/disabled=""/g)?.length).toBe(3);
  });

  it("never shows a stale refusal beside a pending retry", () => {
    const html = render(NEEDS_DECISION, conflict, true);
    expect(html).not.toContain('role="alert"');
    expect(html).toContain("Saving your decision");
  });
});
