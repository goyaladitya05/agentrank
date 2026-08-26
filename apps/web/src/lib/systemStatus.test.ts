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

describe("the schema component", () => {
  it("reports an incompatible schema as unavailable rather than dropping it", async () => {
    // The deploy that starts processes before its migration lands. The database is up and the
    // API is answering, and every merchant request is failing.
    const status = await fetchSystemStatus("https://api.example", () =>
      Promise.resolve(
        Response.json(
          {
            status: "not_ready",
            components: [
              { name: "database", status: "connected", detail: null },
              { name: "schema", status: "incompatible", detail: "expected abc, found def" },
            ],
          },
          { status: 503 },
        ),
      ),
    );

    expect(status.database.state).toBe("connected");
    expect(status.schema.state).toBe("unavailable");
    expect(status.schema.detail).toBe("expected abc, found def");
  });

  it("reads the schema component's own word for connected", async () => {
    const status = await fetchSystemStatus("https://api.example", () =>
      Promise.resolve(
        Response.json({
          status: "ready",
          components: [
            { name: "database", status: "connected", detail: null },
            { name: "schema", status: "compatible", detail: "abc" },
          ],
        }),
      ),
    );

    expect(status.schema.state).toBe("connected");
  });

  it("says a schema the API did not report is unknown rather than fine", async () => {
    const status = await fetchSystemStatus("https://api.example", () =>
      Promise.resolve(
        Response.json({
          status: "ready",
          components: [{ name: "database", status: "connected", detail: null }],
        }),
      ),
    );

    expect(status.schema.state).toBe("unknown");
    expect(status.schema.detail).toBe("not reported by the API");
  });
});
