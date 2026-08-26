import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PublishConfirmation, PublishRepresentation } from "./PublishRepresentation";
import { IDLE_MUTATION, type CompilerMutationState } from "@/lib/compiler-mutation";

function confirmation(state: CompilerMutationState = IDLE_MUTATION, pending = false): string {
  return renderToStaticMarkup(
    <PublishConfirmation
      sourceLabel="voltedge-merchant-source@2"
      action="/fixes"
      state={state}
      pending={pending}
    />,
  );
}

describe("<PublishRepresentation>", () => {
  it("requires an explicit confirmation step before it renders the publish submit", () => {
    const html = renderToStaticMarkup(
      <PublishRepresentation sourceLabel="catalog@2" action={async () => IDLE_MUTATION} />,
    );
    expect(html).toContain("Publish fixes");
    expect(html).not.toContain("Publish fixes</button></div>");
    expect(html).not.toContain('type="submit"');
  });
});

describe("<PublishConfirmation>", () => {
  it("names the source being published and offers a way out", () => {
    const html = confirmation();
    expect(html).toContain("voltedge-merchant-source@2");
    expect(html).toContain("Cancel");
  });

  it("says publication does not rerun a benchmark and promises no performance change", () => {
    const html = confirmation();
    expect(html).toContain("does not rerun a benchmark");
    expect(html).toContain("does not change any price, stock");
    expect(html.toLowerCase()).not.toContain("improve");
    expect(html.toLowerCase()).not.toContain("score");
  });

  it("reports that publishing is in flight and blocks a second submit", () => {
    const html = confirmation(IDLE_MUTATION, true);
    expect(html).toContain("Publishing the representation");
    expect(html).toContain('role="status"');
    expect(html.match(/disabled=""/g)?.length).toBe(2);
  });

  it("states a refused publication inline rather than losing the page", () => {
    const html = confirmation({
      ok: false,
      message: "Some facts still need a decision before this run can be published.",
      stale: true,
      values: null,
    });
    expect(html).toContain('role="alert"');
    expect(html).toContain("Some facts still need a decision");
    expect(html).toContain("The state shown here is current.");
  });
});
