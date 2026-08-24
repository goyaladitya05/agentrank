import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PublishRepresentation } from "./PublishRepresentation";

describe("<PublishRepresentation>", () => {
  it("requires an explicit confirmation step before it renders the publish submit", () => {
    const html = renderToStaticMarkup(
      <PublishRepresentation
        runId="run-1"
        sourceLabel="catalog@2"
        action={async () => ({ ok: true, message: null })}
      />,
    );
    expect(html).toContain("Review publication");
    expect(html).not.toContain("Publish representation</button>");
  });
});
