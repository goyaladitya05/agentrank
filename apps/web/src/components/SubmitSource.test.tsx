import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SubmissionAccepted, SubmitSourceForm } from "./SubmitSource";
import { IDLE_SUBMISSION, type SourceSubmissionState } from "@/lib/source-mutation";

const DOCUMENT = '{\n  "products": [],\n  "policy_text": {}\n}';

function form(
  state: SourceSubmissionState = IDLE_SUBMISSION,
  pending = false,
  hasCurrentSource = true,
): string {
  return renderToStaticMarkup(
    <SubmitSourceForm
      initialDocument={DOCUMENT}
      hasCurrentSource={hasCurrentSource}
      action="/sources"
      state={state}
      pending={pending}
    />,
  );
}

function accepted(state: Partial<SourceSubmissionState>): string {
  return renderToStaticMarkup(
    <SubmissionAccepted state={{ ...IDLE_SUBMISSION, ok: true, ...state }} />,
  );
}

describe("<SubmitSourceForm>", () => {
  it("prefills the merchant's current document and labels the editor", () => {
    const html = form();
    expect(html).toContain('id="source-document"');
    expect(html).toContain('for="source-document"');
    expect(html).toContain("policy_text");
    expect(html).toContain("Edit it and submit to create a newer snapshot");
  });

  it("says what submitting does and does not do, and promises no improvement", () => {
    const html = form().toLowerCase();
    expect(html).toContain("your existing snapshots do not change");
    expect(html).toContain("nothing is compiled here");
    expect(html).toContain("does not change any price, stock level or order");
    expect(html).not.toContain("improve");
    expect(html).not.toContain("optimi");
    expect(html).not.toContain("unlock");
  });

  it("tells a merchant with no source that this creates their first snapshot", () => {
    const html = form(IDLE_SUBMISSION, false, false);
    expect(html).toContain("You have no source snapshot yet");
  });

  it("reports that a submission is in flight and blocks a second submit", () => {
    const html = form(IDLE_SUBMISSION, true);
    expect(html).toContain("Storing your source document");
    expect(html).toContain('role="status"');
    expect(html).toContain('disabled=""');
  });

  it("keeps what the merchant typed when the document was refused", () => {
    const typed = '{ "products": [ }';
    const html = form({
      ...IDLE_SUBMISSION,
      message: "This is not valid JSON: Unexpected token",
      values: { document: typed },
    });
    expect(html).toContain('role="alert"');
    expect(html).toContain("This is not valid JSON");
    expect(html).toContain("&quot;products&quot;: [ }");
    expect(html).toContain('aria-describedby="source-document-error"');
  });

  it("adds the state sentence only to a refusal caused by state moving", () => {
    const stale = form({
      ...IDLE_SUBMISSION,
      message: "Reload to see your current source.",
      stale: true,
    });
    expect(stale).toContain("The state shown here is current.");
    const unknown = form({
      ...IDLE_SUBMISSION,
      message: "The console could not reach AgentRank",
      stale: true,
      unknown: true,
    });
    expect(unknown).not.toContain("The state shown here is current.");
  });
});

describe("<SubmissionAccepted>", () => {
  it("says a snapshot was created and that nothing has been compiled yet", () => {
    const html = accepted({
      createdSnapshot: true,
      snapshotId: "01a03000-0000-7000-8000-000000000001",
    });
    expect(html).toContain("Source snapshot created.");
    expect(html).toContain("Nothing has been compiled yet.");
    expect(html).toContain("/sources/01a03000-0000-7000-8000-000000000001");
    expect(html).toContain('role="status"');
  });

  it("says nothing was written when the document matched the current snapshot", () => {
    const html = accepted({
      createdSnapshot: false,
      snapshotId: "01a03000-0000-7000-8000-000000000002",
    });
    expect(html).toContain("no new snapshot was created");
    expect(html).not.toContain("Source snapshot created.");
    expect(html).toContain("Open your current source snapshot");
  });
});
