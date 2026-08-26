/**
 * Readiness for the console process.
 *
 * Ready means this console can serve a signed in merchant screen, which takes two things: a
 * configuration it can issue and resolve sessions with, and an AgentRank API that can serve one.
 *
 * The API side is its readiness endpoint and not its liveness endpoint. Liveness says a process
 * is answering, which is true of an API running against a schema its build was not written for,
 * and every merchant page this console renders would then fail. An orchestrator gated on this
 * would have routed browser traffic straight into it. Readiness costs one connection check and
 * one revision read, which is what a probe on a schedule should be doing.
 *
 * What comes back names components and states. It never carries a configured value, a URL, a
 * credential or an upstream's own error text, because a readiness endpoint is usually the one
 * thing on a deployment that is reachable without authenticating.
 */

import { apiBaseUrl } from "@/lib/config";
import { inspectConfiguration } from "@/lib/configuration";

export const dynamic = "force-dynamic";

/** How long the API gets to answer its readiness endpoint before this reports it unavailable. */
const PROBE_TIMEOUT_MS = 2_000;

interface Component {
  readonly name: string;
  readonly status: "ok" | "unavailable" | "misconfigured";
  readonly detail?: string;
}

async function apiComponent(): Promise<Component> {
  try {
    const response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}/ready`, {
      cache: "no-store",
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!response.ok) {
      // The API's own reason is not carried across. It names a migration revision, and a probe
      // nobody has to authenticate for is not where a deployment's schema version belongs.
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
