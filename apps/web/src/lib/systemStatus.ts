/**
 * Reads the backend readiness endpoint and turns it into something the console can
 * render. The rule this module exists to enforce: never report a component as connected
 * when the request that would prove it did not succeed.
 */

export type ComponentState = "connected" | "unavailable" | "unknown";

export interface ComponentStatus {
  readonly name: string;
  readonly state: ComponentState;
  readonly detail: string | null;
}

export interface SystemStatus {
  readonly api: ComponentStatus;
  readonly database: ComponentStatus;
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

interface ReadinessComponent {
  readonly name: string;
  readonly status: string;
  readonly detail: string | null;
}

interface ReadinessPayload {
  readonly status: string;
  readonly components: readonly ReadinessComponent[];
}

function isReadinessComponent(value: unknown): value is ReadinessComponent {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.name === "string" &&
    typeof candidate.status === "string" &&
    (candidate.detail === null || typeof candidate.detail === "string")
  );
}

function isReadinessPayload(value: unknown): value is ReadinessPayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.status === "string" &&
    Array.isArray(candidate.components) &&
    candidate.components.every(isReadinessComponent)
  );
}

function toComponentState(status: string): ComponentState {
  if (status === "connected" || status === "unavailable") {
    return status;
  }
  return "unknown";
}

/**
 * The API did not answer usefully, so nothing behind it can be reported as working.
 * The database is unknown rather than unavailable: it may well be up.
 */
function apiUnreachable(detail: string): SystemStatus {
  return {
    api: { name: "API", state: "unavailable", detail },
    database: { name: "Database", state: "unknown", detail: null },
  };
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : "request failed";
}

export async function fetchSystemStatus(
  baseUrl: string,
  fetchImpl: FetchLike = fetch,
): Promise<SystemStatus> {
  const url = `${baseUrl.replace(/\/+$/, "")}/ready`;

  let response: Response;
  try {
    response = await fetchImpl(url, { cache: "no-store" });
  } catch (error) {
    return apiUnreachable(describe(error));
  }

  // 200 and 503 are both real readiness answers. Anything else means the API itself is
  // not behaving, whatever the body happens to contain.
  if (response.status !== 200 && response.status !== 503) {
    return apiUnreachable(`HTTP ${String(response.status)}`);
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return apiUnreachable("response body was not JSON");
  }

  if (!isReadinessPayload(payload)) {
    return apiUnreachable("unexpected response shape");
  }

  const database = payload.components.find((component) => component.name === "database");

  return {
    api: { name: "API", state: "connected", detail: null },
    database: database
      ? {
          name: "Database",
          state: toComponentState(database.status),
          detail: database.detail,
        }
      : { name: "Database", state: "unknown", detail: "not reported by the API" },
  };
}
