/**
 * Readiness for the console process.
 *
 * Ready means this console can serve a signed in merchant screen, which takes two things: a
 * configuration it can issue and resolve sessions with, and an AgentRank API that answers. Both
 * are checked, and neither check costs anything a probe should not be doing on a schedule: the
 * API side is its liveness endpoint, which touches no database and no model provider.
 *
 * What comes back names components and states. It never carries a configured value, a URL, a
 * credential or an upstream's own error text, because a readiness endpoint is usually the one
 * thing on a deployment that is reachable without authenticating.
 */

import { apiBaseUrl } from "@/lib/config";
import { inspectConfiguration } from "@/lib/configuration";

export const dynamic = "force-dynamic";

/** How long the API gets to answer its liveness endpoint before this reports it unavailable. */
const PROBE_TIMEOUT_MS = 2_000;

interface Component {
  readonly name: string;
  readonly status: "ok" | "unavailable" | "misconfigured";
  readonly detail?: string;
}

async function apiComponent(): Promise<Component> {
  try {
    const response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!response.ok) {
      return {
        name: "api",
        status: "unavailable",
        detail: `answered HTTP ${String(response.status)}`,
      };
    }
    return { name: "api", status: "ok" };
  } catch {
    // Deliberately not the thrown error. A fetch failure message carries the host and port it
    // tried, and a probe nobody has to authenticate for is not where that belongs.
    return { name: "api", status: "unavailable", detail: "did not answer" };
  }
}

export async function GET(): Promise<Response> {
  const report = inspectConfiguration();
  const configuration: Component =
    report.problems.length === 0
      ? { name: "configuration", status: "ok" }
      : {
          // The problems are already written to name variables and never values, which is what
          // makes them safe to return here as well as to log at startup.
          name: "configuration",
          status: "misconfigured",
          detail: report.problems.join("; "),
        };

  const components = [configuration, await apiComponent()];
  const ready = components.every((component) => component.status === "ok");
  return Response.json(
    { status: ready ? "ready" : "not_ready", components },
    { status: ready ? 200 : 503 },
  );
}
