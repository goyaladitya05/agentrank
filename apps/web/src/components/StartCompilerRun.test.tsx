import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  AlreadyCompiled,
  CompileAccepted,
  CompileConfirmation,
  StartCompilerRun,
} from "./StartCompilerRun";
import { IDLE_COMPILE, type CompileState } from "@/lib/source-mutation";

function confirmation(state: CompileState = IDLE_COMPILE, pending = false): string {
  return renderToStaticMarkup(
    <CompileConfirmation
      sourceLabel="merchant-source@2"
      action="/sources"
      state={state}
      pending={pending}
    />,
  );
}

describe("<StartCompilerRun>", () => {
  it("names the snapshot it would read before it is run", () => {
    const html = renderToStaticMarkup(
      <StartCompilerRun
        sourceLabel="merchant-source@3"
        compilable
        existingRunId={null}
        action={async () => IDLE_COMPILE}
      />,
    );
    expect(html).toContain("merchant-source@3");
    expect(html).toContain("Run the compiler");
  });

  it("keeps a refusal visible even when the snapshot was compiled elsewhere meanwhile", () => {
    const html = renderToStaticMarkup(
      <StartCompilerRun
        sourceLabel="merchant-source@3"
        compilable={false}
        existingRunId="01a03000-0000-7000-8000-00000000000b"
        action={async () => IDLE_COMPILE}
      />,
    );
    expect(html).toContain("already been read by the compiler");
  });

  it("offers the run that exists rather than a second reading of one snapshot", () => {
    const html = renderToStaticMarkup(
      <StartCompilerRun
        sourceLabel="merchant-source@3"
        compilable={false}
        existingRunId="01a03000-0000-7000-8000-00000000000b"
        action={async () => IDLE_COMPILE}
      />,
    );
    expect(html).toContain("already been read by the compiler");
    expect(html).toContain("/fixes/01a03000-0000-7000-8000-00000000000b");
    expect(html).not.toContain("Run the compiler</button>");
  });
});

describe("<AlreadyCompiled>", () => {
  it("says why compiling again would change nothing", () => {
    const html = renderToStaticMarkup(<AlreadyCompiled existingRunId={null} />);
    expect(html).toContain("The compiler is deterministic");
    expect(html).toContain("supply newer source evidence");
  });
});

describe("<CompileConfirmation>", () => {
  it("states that a run publishes nothing and starts no benchmark", () => {
    const html = confirmation();
    expect(html).toContain("publishes nothing and starts no benchmark");
    expect(html).toContain("No price, stock level or order changes.");
    expect(html).toContain("asking twice cannot produce two");
  });

  it("promises no benchmark improvement", () => {
    const html = confirmation().toLowerCase();
    expect(html).not.toContain("improve");
    expect(html).not.toContain("optimi");
    expect(html).not.toContain("score");
    expect(html).not.toContain("revenue");
  });

  it("reports that the compiler is running and blocks a second submit", () => {
    const html = confirmation(IDLE_COMPILE, true);
    expect(html).toContain("Running the compiler");
    expect(html).toContain('role="status"');
    expect(html).toContain('disabled=""');
  });

  it("states a refusal inline rather than losing the page", () => {
    const html = confirmation({
      ...IDLE_COMPILE,
      message: "This source snapshot is no longer available.",
      stale: true,
    });
    expect(html).toContain('role="alert"');
    expect(html).toContain("This source snapshot is no longer available.");
    expect(html).toContain("The state shown here is current.");
  });
});

describe("<CompileAccepted>", () => {
  function accepted(state: Partial<CompileState>): string {
    return renderToStaticMarkup(
      <CompileAccepted
        state={{
          ...IDLE_COMPILE,
          ok: true,
          runId: "01a03000-0000-7000-8000-00000000000a",
          runStatus: "COMPLETED",
          pendingReviews: 0,
          ...state,
        }}
      />,
    );
  }

  it("takes the merchant into the review workflow when there is something to review", () => {
    const html = accepted({ pendingReviews: 2 });
    expect(html).toContain("2 facts need your decision");
    expect(html).toContain("Review this compiler run");
    expect(html).toContain("/fixes/01a03000-0000-7000-8000-00000000000a");
    expect(html).toContain('role="status"');
  });

  it("counts one fact in the singular", () => {
    expect(accepted({ pendingReviews: 1 })).toContain("1 fact needs your decision");
  });

  it("does not claim facts are waiting when nothing needs a decision", () => {
    const html = accepted({ pendingReviews: 0 });
    expect(html).toContain("Nothing it proposed needs a decision from you");
    expect(html).not.toContain("need your decision");
    expect(html).toContain("Open this compiler run");
  });

  it("says a run that could not read its snapshot did not read it", () => {
    const html = accepted({ runStatus: "FAILED", pendingReviews: 0 });
    expect(html).toContain("could not read this snapshot");
    expect(html).not.toContain("Compiler run finished");
  });

  it("says a run left unfinished by an older build never finished", () => {
    // Nothing can create either state now: a compiler run is written settled in one transaction.
    // Rows that already carry one stay readable, and this is what the console says about them.
    for (const runStatus of ["PENDING", "RUNNING"]) {
      const html = accepted({ runStatus, pendingReviews: 0 });
      expect(html).toContain("never finished");
      expect(html).not.toContain("could not read this snapshot");
      expect(html).not.toContain("Compiler run finished");
    }
  });

  it("says an already published run cannot change", () => {
    const html = accepted({ published: true });
    expect(html).toContain("already compiled and published");
    expect(html).toContain("cannot change");
  });
});
