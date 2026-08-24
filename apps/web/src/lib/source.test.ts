import { describe, expect, it } from "vitest";

import { DecodeError } from "@/lib/insights/decode";
import {
  decodeSourceOverview,
  decodeSourceSnapshot,
  decodeSourceSubmission,
  documentText,
  originLabel,
} from "@/lib/source";

const SUMMARY = {
  source_snapshot_id: "01a03000-0000-7000-8000-000000000001",
  source_label: "merchant-source@2",
  source_key: "merchant-source",
  source_version: 2,
  content_hash: `sha256:${"0".repeat(64)}`,
  created_at: "2026-08-25T10:00:00Z",
  origin: "MERCHANT_CONSOLE",
  product_count: 2,
  variant_count: 4,
  policy_count: 3,
  compiler_run_count: 1,
  published_representation_count: 0,
  is_current: true,
};

describe("decodeSourceOverview", () => {
  it("reads a history and which snapshot is current", () => {
    const overview = decodeSourceOverview({
      current_source_snapshot_id: SUMMARY.source_snapshot_id,
      snapshots: [SUMMARY],
    });
    expect(overview.current_source_snapshot_id).toBe(SUMMARY.source_snapshot_id);
    expect(overview.snapshots[0]?.variant_count).toBe(4);
  });

  it("reads an empty history as empty rather than as a failure", () => {
    const overview = decodeSourceOverview({ current_source_snapshot_id: null, snapshots: [] });
    expect(overview.current_source_snapshot_id).toBeNull();
    expect(overview.snapshots).toHaveLength(0);
  });

  it("refuses a count that is not an integer", () => {
    expect(() =>
      decodeSourceOverview({
        current_source_snapshot_id: null,
        snapshots: [{ ...SUMMARY, variant_count: "four" }],
      }),
    ).toThrow(DecodeError);
  });
});

describe("decodeSourceSnapshot", () => {
  const snapshot = {
    summary: SUMMARY,
    document: { products: [], policy_text: {} },
    fields: [{ field: "policy_text.warranty", excerpt: "one-year limited", truncated: false }],
    compiler_runs: [
      {
        run_id: "01a03000-0000-7000-8000-00000000000a",
        status: "COMPLETED",
        configuration_digest: `sha256:${"1".repeat(64)}`,
        created_at: "2026-08-25T10:05:00Z",
        completed_at: "2026-08-25T10:05:01Z",
        error_code: null,
        review_required_count: 2,
        reviewed_count: 0,
        published_representation_id: null,
      },
    ],
    compilable: false,
    existing_run_id: "01a03000-0000-7000-8000-00000000000a",
  };

  it("reads the document, its addressable fields and every run over it", () => {
    const decoded = decodeSourceSnapshot(snapshot);
    expect(decoded.fields[0]?.field).toBe("policy_text.warranty");
    expect(decoded.compiler_runs[0]?.review_required_count).toBe(2);
    expect(decoded.compilable).toBe(false);
    expect(decoded.existing_run_id).toBe("01a03000-0000-7000-8000-00000000000a");
  });

  it("refuses a document that is not an object", () => {
    expect(() => decodeSourceSnapshot({ ...snapshot, document: [] })).toThrow(DecodeError);
  });
});

describe("decodeSourceSubmission", () => {
  it("reads whether the command created a snapshot or matched the current one", () => {
    const matched = decodeSourceSubmission({
      submission_id: "01a03000-0000-7000-8000-000000000009",
      request_key: "a-request-key",
      created_snapshot: false,
      snapshot: SUMMARY,
    });
    expect(matched.created_snapshot).toBe(false);
    expect(matched.snapshot.source_label).toBe("merchant-source@2");
  });

  it("refuses an outcome that does not say what it did", () => {
    expect(() =>
      decodeSourceSubmission({
        submission_id: "01a03000-0000-7000-8000-000000000009",
        request_key: "a-request-key",
        created_snapshot: "yes",
        snapshot: SUMMARY,
      }),
    ).toThrow(DecodeError);
  });
});

describe("originLabel", () => {
  it("names each mechanism in words and leaves an unknown one readable", () => {
    expect(originLabel("MERCHANT_CONSOLE")).toBe("Submitted in the console");
    expect(originLabel("OPERATOR_FIXTURE")).toBe("Published by an operator");
    expect(originLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });
});

describe("documentText", () => {
  it("renders a document as the text a merchant edits", () => {
    expect(documentText({ products: [], policy_text: {} })).toBe(
      '{\n  "products": [],\n  "policy_text": {}\n}',
    );
  });
});
