import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CompileAccepted, CompileConfirmation, StartCompilerRun } from "./StartCompilerRun";
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
      <StartCompilerRun sourceLabel="merchant-source@3" action={async () => IDLE_COMPILE} />,
    );
    expect(html).toContain("merchant-source@3");
    expect(html).toContain("Run the compiler");
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
  it("takes the merchant into the review workflow rather than claiming an outcome", () => {
    const html = renderToStaticMarkup(
      <CompileAccepted
        state={{ ...IDLE_COMPILE, ok: true, runId: "01a03000-0000-7000-8000-00000000000a" }}
      />,
    );
    expect(html).toContain("waiting for your review");
    expect(html).toContain("/compiler/runs/01a03000-0000-7000-8000-00000000000a");
    expect(html).toContain('role="status"');
  });
});
