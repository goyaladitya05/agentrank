/**
 * Calling the AgentRank API from the Next.js server, with the merchant credential.
 *
 * The credential is read from the server environment and is never sent to the browser. The
 * console fetches the API server side for exactly this reason, which is the same decision the
 * status page made in Phase 0 and the reason there is no CORS configuration anywhere.
 *
 * A missing credential is a configuration error with a sentence rather than a 401 the browser
 * has to interpret. The console is an operations tool and the person running it is the person
 * who can fix it.
 */

import { apiBaseUrl } from "@/lib/config";

const CREDENTIAL_VARIABLE = "AGENTRANK_MERCHANT_API_KEY";

export interface ApiResult {
  readonly status: number;
  readonly body: unknown;
}

export function merchantCredential(): string | null {
  const token = process.env[CREDENTIAL_VARIABLE];
  return token && token.length > 0 ? token : null;
}

/**
 * One call to the AgentRank API, returning the status and the parsed body rather than throwing.
 *
 * The status is returned because the API answers meaningfully with several of them: 200 for a
 * refusal that is an ordinary fact, 409 for state that says no, 502 for a gateway that did not
 * cooperate. A wrapper that threw on everything above 299 would flatten those into one message.
 */
export async function callApi(
  path: string,
  init: { readonly method: string; readonly body?: unknown },
): Promise<ApiResult> {
  const token = merchantCredential();
  if (token === null) {
    throw new Error(`${CREDENTIAL_VARIABLE} is not set, so the console cannot call the API`);
  }

  // `exactOptionalPropertyTypes` is on, so an absent body is an absent key rather than a key
  // holding undefined. The distinction is the whole reason that flag exists.
  const request: RequestInit = {
    method: init.method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    cache: "no-store",
  };
  if (init.body !== undefined) {
    request.body = JSON.stringify(init.body);
  }

  const response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}${path}`, request);

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return { status: response.status, body };
}
