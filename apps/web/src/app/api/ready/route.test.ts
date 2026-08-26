import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET } from "./route";

/**
 * The console's own readiness, which an orchestrator gates browser traffic on.
 *
 * The whole substance of this route is which endpoint it asks the API for. It used to ask
 * `/health`, which answers from the process alone: an API running against a schema its build was
 * not written for answers it instantly, and every merchant page the console renders then fails.
 * A console gated on that is a console an orchestrator routes traffic into.
 *
 * Untested until now, which meant a refactor could put it back with every check still green.
 */

const CONFIGURED = {
  AGENTRANK_API_BASE_URL: "http://api.example",
  AGENTRANK_CONSOLE_SESSION_SECRET: "s".repeat(64),
};

let asked: string[];

beforeEach(() => {
  asked = [];
  for (const [name, value] of Object.entries(CONFIGURED)) {
    vi.stubEnv(name, value);
  }
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

function answering(status: number): void {
  vi.stubGlobal("fetch", (input: string) => {
    asked.push(String(input));
    return Promise.resolve(new Response("{}", { status }));
  });
}

describe("the console's readiness probe", () => {
  it("asks the API whether it can serve, not whether it is alive", async () => {
    answering(200);

    const response = await GET();

    expect(asked).toEqual(["http://api.example/ready"]);
    expect(response.status).toBe(200);
    expect(((await response.json()) as { status: string }).status).toBe("ready");
  });

  it("is not ready when the API is not ready", async () => {
    // The out of order deploy: the API is answering and cannot serve a merchant page.
    answering(503);

    const response = await GET();

    expect(response.status).toBe(503);
    const body = (await response.json()) as {
      components: { name: string; status: string; detail?: string }[];
    };
    const api = body.components.find((component) => component.name === "api");
    expect(api?.status).toBe("unavailable");
    // The API's own reason names a migration revision, and this endpoint takes no credential.
    expect(JSON.stringify(body)).not.toContain("revision");
  });

  it("separates an API that did not answer in time from one that could not be reached", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new DOMException("timed out", "TimeoutError")));
    const timedOut = await GET();

    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("fetch failed")));
    const unreachable = await GET();

    const detail = async (response: Response) =>
      (
        (await response.json()) as { components: { name: string; detail?: string }[] }
      ).components.find((component) => component.name === "api")?.detail;

    expect(await detail(timedOut)).toBe("did not answer in time");
    expect(await detail(unreachable)).toBe("could not be reached");
  });

  it("never carries a configured value into a body nobody has to authenticate for", async () => {
    answering(200);

    const body = await (await GET()).text();

    expect(body).not.toContain("api.example");
    expect(body).not.toContain(CONFIGURED.AGENTRANK_CONSOLE_SESSION_SECRET);
  });
});
