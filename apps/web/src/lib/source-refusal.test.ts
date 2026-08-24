import { describe, expect, it } from "vitest";

import { COMPILE_REFUSALS, SOURCE_REFUSALS, fieldMessages, refusal } from "@/lib/source-refusal";

describe("refusal", () => {
  it("maps a code this console has words for", () => {
    const answer = refusal(
      409,
      { error: "source_request_key_reused", detail: "machine words" },
      SOURCE_REFUSALS,
    );
    expect(answer.message).toContain("Reload to start a new submission");
    expect(answer.message).not.toContain("machine words");
    expect(answer.stale).toBe(true);
  });

  it("names the fields of a schema refusal rather than flattening it", () => {
    const answer = refusal(
      422,
      {
        error: "invalid_request",
        detail: "body.products.0.title: too long",
        fields: [
          { location: ["body", "products", "0", "title"], message: "too long" },
          { location: ["body", "policy_text"], message: "not an object" },
        ],
      },
      SOURCE_REFUSALS,
    );
    expect(answer.message).toContain("products.0.title: too long.");
    expect(answer.message).toContain("policy_text: not an object.");
    expect(answer.stale).toBe(false);
  });

  it("survives a body no gateway promised to send", () => {
    // A proxy answering 502 with HTML, an empty body, or nothing parses to null here. Throwing
    // would take the merchant's typed document with it, which is worse than any message.
    for (const payload of [null, undefined, "<html>502</html>", [], 7]) {
      const answer = refusal(502, payload, SOURCE_REFUSALS);
      expect(answer.message).toBe("AgentRank refused this request (HTTP 502).");
      expect(answer.stale).toBe(false);
    }
  });

  it("falls back to the API's own sentence before inventing one", () => {
    const answer = refusal(422, { detail: "a sentence the API wrote" }, SOURCE_REFUSALS);
    expect(answer.message).toBe("a sentence the API wrote");
  });

  it("treats a moved world as stale and a wrong request as not", () => {
    expect(refusal(404, {}, COMPILE_REFUSALS).stale).toBe(true);
    expect(refusal(409, {}, COMPILE_REFUSALS).stale).toBe(true);
    expect(refusal(422, {}, COMPILE_REFUSALS).stale).toBe(false);
    expect(refusal(500, {}, COMPILE_REFUSALS).stale).toBe(false);
  });

  it("uses the map it was given rather than one shared vocabulary", () => {
    expect(refusal(404, { error: "not_found" }, COMPILE_REFUSALS).message).toContain(
      "source snapshot is no longer available",
    );
    expect(refusal(404, { error: "not_found" }, SOURCE_REFUSALS).message).toBe(
      "Reload this page and try again.",
    );
  });
});

describe("fieldMessages", () => {
  it("reports at most three, so a refusal does not become a wall", () => {
    const many = Array.from({ length: 20 }, (_, index) => ({
      location: ["body", String(index)],
      message: "wrong",
    }));
    expect(fieldMessages(many)).toHaveLength(3);
  });

  it("ignores entries that say nothing", () => {
    expect(fieldMessages([{ location: ["body"] }, null, 4, "x"])).toEqual([]);
    expect(fieldMessages(undefined)).toEqual([]);
    expect(fieldMessages({ location: [] })).toEqual([]);
  });

  it("keeps a message that names no field readable", () => {
    expect(fieldMessages([{ location: ["body"], message: "not an object" }])).toEqual([
      "not an object.",
    ]);
  });
});
