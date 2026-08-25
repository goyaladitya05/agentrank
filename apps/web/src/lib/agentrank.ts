/**
 * Calling the AgentRank API from the Next.js server, with the console session credential.
 *
 * The credential is derived from the browser's session cookie and is never sent to browser
 * JavaScript. A route handler supplies it after it has checked the session and origin. It is not
 * a merchant API key: this console holds none. See `@/lib/auth/session`.
 */

import { apiBaseUrl } from "@/lib/config";

export interface ApiResult {
  readonly status: number;
  readonly body: unknown;
}

/**
 * One call to the AgentRank API, returning the status and the parsed body rather than throwing.
 *
 * The status is returned because the API answers meaningfully with several of them: 200 for a
 * refusal that is an ordinary fact, 409 for state that says no, 502 for a gateway that did not
 * cooperate. A wrapper that threw on everything above 299 would flatten those into one message.
 */
export async function callApi(
  credential: string,
  path: string,
  init: { readonly method: string; readonly body?: unknown },
): Promise<ApiResult> {
  // `exactOptionalPropertyTypes` is on, so an absent body is an absent key rather than a key
  // holding undefined. The distinction is the whole reason that flag exists.
  const request: RequestInit = {
    method: init.method,
    headers: {
      Authorization: `Bearer ${credential}`,
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
