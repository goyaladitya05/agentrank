import { describe, expect, it } from "vitest";

import { fetchSystemStatus, type FetchLike } from "@/lib/systemStatus";

function respondWith(status: number, body: unknown): FetchLike {
  return () => Promise.resolve(new Response(JSON.stringify(body), { status }));
}

describe("fetchSystemStatus", () => {
  it("reports both components connected when the API is ready", async () => {
    const status = await fetchSystemStatus(
      "http://api.test",
      respondWith(200, {
        status: "ready",
        components: [{ name: "database", status: "connected", detail: null }],
      }),
    );

    expect(status.api.state).toBe("connected");
    expect(status.database.state).toBe("connected");
  });

  it("reports the database unavailable when the API answers 503", async () => {
    const status = await fetchSystemStatus(
      "http://api.test",
      respondWith(503, {
        status: "not_ready",
        components: [{ name: "database", status: "unavailable", detail: "OperationalError" }],
      }),
    );

    expect(status.api.state).toBe("connected");
    expect(status.database.state).toBe("unavailable");
    expect(status.database.detail).toBe("OperationalError");
  });

  it("reports the database unknown, not connected, when the request fails", async () => {
    const status = await fetchSystemStatus("http://api.test", () =>
      Promise.reject(new Error("fetch failed")),
    );

    expect(status.api.state).toBe("unavailable");
    expect(status.api.detail).toBe("fetch failed");
    expect(status.database.state).toBe("unknown");
  });

  it("treats an unexpected payload as an API failure", async () => {
    const status = await fetchSystemStatus("http://api.test", respondWith(200, { ok: true }));

    expect(status.api.state).toBe("unavailable");
    expect(status.database.state).toBe("unknown");
  });

  it("treats an unexpected HTTP status as an API failure", async () => {
    const status = await fetchSystemStatus("http://api.test", respondWith(500, {}));

    expect(status.api.state).toBe("unavailable");
    expect(status.api.detail).toBe("HTTP 500");
  });

  it("does not produce a double slash when the base URL has a trailing slash", async () => {
    let requested = "";
    const status = await fetchSystemStatus("http://api.test/", (input) => {
      requested = input;
      return Promise.resolve(
        new Response(JSON.stringify({ status: "ready", components: [] }), { status: 200 }),
      );
    });

    expect(requested).toBe("http://api.test/ready");
    expect(status.database.state).toBe("unknown");
  });
});
