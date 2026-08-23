import { describe, expect, it } from "vitest";

import { fetchInsight } from "./client";
import { decodeMerchantOverview, decodeRunSummary } from "./decode";
import { OVERVIEW_FIXTURE } from "./fixtures";

const decode = (value: unknown) => value;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchReturning(response: Response): typeof fetch {
  return async () => response;
}

describe("fetchInsight", () => {
  const options = { baseUrl: "http://api.test", apiKey: "ar_dev_key" };

  it("attaches the credential as a bearer token and decodes a successful answer", async () => {
    let seenAuthorization: string | null = null;
    const fetchImpl: typeof fetch = async (input, init) => {
      seenAuthorization = new Request(input, init).headers.get("Authorization");
      return jsonResponse(200, OVERVIEW_FIXTURE.runs[0]);
    };
    const outcome = await fetchInsight("/api/v1/insights/runs?limit=1", decodeRunSummary, {
      ...options,
      fetchImpl,
    });
    expect(seenAuthorization).toBe("Bearer ar_dev_key");
    expect(outcome).toEqual({
      ok: true,
      data: decodeRunSummary(OVERVIEW_FIXTURE.runs[0]),
    });
  });

  it("answers unauthenticated without calling the API when the console holds no credential", async () => {
    let called = false;
    const fetchImpl: typeof fetch = async () => {
      called = true;
      return jsonResponse(200, {});
    };
    const outcome = await fetchInsight("/api/v1/insights/overview", decode, {
      ...options,
      apiKey: null,
      fetchImpl,
    });
    expect(called).toBe(false);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.reason).toBe("unauthenticated");
    }
  });

  it("classifies 401 and 403 as credential rejections", async () => {
    for (const status of [401, 403]) {
      const outcome = await fetchInsight("/api/v1/insights/overview", decode, {
        ...options,
        fetchImpl: fetchReturning(jsonResponse(status, { detail: "no" })),
      });
      expect(outcome.ok).toBe(false);
      if (!outcome.ok) {
        expect(
          outcome.failure.reason === "unauthenticated" || outcome.failure.reason === "forbidden",
        ).toBe(true);
      }
    }
  });

  it("classifies 404 as not found and keeps the message merchant scoped", async () => {
    const outcome = await fetchInsight("/api/v1/insights/runs/nope", decode, {
      ...options,
      fetchImpl: fetchReturning(jsonResponse(404, { resource: "benchmark_run" })),
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.reason).toBe("notFound");
      expect(outcome.failure.message).toMatch(/belongs to your merchant/);
    }
  });

  it("classifies other error statuses with their HTTP code", async () => {
    const outcome = await fetchInsight("/api/v1/insights/overview", decode, {
      ...options,
      fetchImpl: fetchReturning(jsonResponse(502, {})),
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure).toEqual({
        reason: "apiError",
        status: 502,
        message: "The AgentRank API answered HTTP 502.",
      });
    }
  });

  it("classifies transport failures as network errors without leaking the credential", async () => {
    const outcome = await fetchInsight("/api/v1/insights/overview", decode, {
      baseUrl: "http://127.0.0.1:1",
      apiKey: "ar_dev_secret_value",
      fetchImpl: (async () => {
        throw new Error("connection refused");
      }) as typeof fetch,
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.reason).toBe("networkError");
      expect(outcome.failure.message).toBe("connection refused");
      expect(outcome.failure.message).not.toContain("ar_dev_secret_value");
    }
  });

  it("classifies undecodable bodies as invalid responses naming the field", async () => {
    const broken = { ...OVERVIEW_FIXTURE.runs[0], unsafe_attempts: null };
    const outcome = await fetchInsight("/api/v1/insights/runs?limit=1", decodeRunSummary, {
      ...options,
      fetchImpl: fetchReturning(jsonResponse(200, broken)),
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.reason).toBe("invalidResponse");
      expect(outcome.failure.message).toContain("unsafe_attempts");
    }
  });

  it("classifies non JSON bodies on success statuses as invalid responses", async () => {
    const response = new Response("<html>gateway</html>", { status: 200 });
    const outcome = await fetchInsight("/api/v1/insights/overview", decodeMerchantOverview, {
      ...options,
      fetchImpl: fetchReturning(response),
    });
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.reason).toBe("invalidResponse");
    }
  });
});
