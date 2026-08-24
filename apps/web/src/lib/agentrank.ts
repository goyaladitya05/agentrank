/**
 * Calling the AgentRank API from the Next.js server, with the merchant credential.
 *
 * The credential comes from the authenticated server-side browser session and is never sent to
 * browser JavaScript.  A route handler supplies it after it has checked the session and origin.
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
  apiKey: string,
  path: string,
  init: { readonly method: string; readonly body?: unknown },
): Promise<ApiResult> {
  // `exactOptionalPropertyTypes` is on, so an absent body is an absent key rather than a key
  // holding undefined. The distinction is the whole reason that flag exists.
  const request: RequestInit = {
    method: init.method,
    headers: {
      Authorization: `Bearer ${apiKey}`,
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
