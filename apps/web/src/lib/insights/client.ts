/**
 * Calling the authenticated insights API from the Next.js server and classifying every
 * answer the console can act on.
 *
 * Like the rest of the console, these calls run on the server only: the merchant
 * credential never reaches a browser, there is no API URL in client JavaScript and there
 * is no CORS configuration to maintain. Every failure mode a page can meaningfully render
 * is a named variant, so a route can tell "your session is not valid" from "that run does
 * not exist" from "the API is down" without parsing messages.
 */

import { DecodeError } from "./decode";

export type InsightsFailure =
  | { readonly reason: "unauthenticated"; readonly message: string }
  | { readonly reason: "forbidden"; readonly message: string }
  | { readonly reason: "notFound"; readonly message: string }
  | { readonly reason: "apiError"; readonly status: number; readonly message: string }
  | { readonly reason: "networkError"; readonly message: string }
  | { readonly reason: "invalidResponse"; readonly message: string };

export type InsightsOutcome<T> =
  | { readonly ok: true; readonly data: T }
  | { readonly ok: false; readonly failure: InsightsFailure };

export interface FetchOptions {
  /** Absolute base URL of the AgentRank API, from server configuration only. */
  readonly baseUrl: string;
  /** The console session credential, or null when the browser holds no usable session. */
  readonly credential: string | null;
  /** Injectable for tests; production always uses the platform fetch. */
  readonly fetchImpl?: typeof fetch;
}

export async function fetchInsight<T>(
  path: string,
  decode: (value: unknown) => T,
  options: FetchOptions,
): Promise<InsightsOutcome<T>> {
  const doFetch = options.fetchImpl ?? fetch;
  if (options.credential === null) {
    return {
      ok: false,
      failure: {
        reason: "unauthenticated",
        message: "The console holds no merchant session. Sign in to continue.",
      },
    };
  }

  const url = `${options.baseUrl.replace(/\/+$/, "")}${path}`;
  let response: Response;
  try {
    response = await doFetch(url, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${options.credential}`,
        Accept: "application/json",
      },
      cache: "no-store",
    });
  } catch (error) {
    return {
      ok: false,
      failure: {
        reason: "networkError",
        message: error instanceof Error ? error.message : "the request could not be sent",
      },
    };
  }

  if (response.status === 401 || response.status === 403) {
    return {
      ok: false,
      failure: {
        reason: response.status === 403 ? "forbidden" : "unauthenticated",
        message: "The AgentRank API rejected this merchant credential.",
      },
    };
  }
  if (response.status === 404) {
    return {
      ok: false,
      failure: {
        reason: "notFound",
        message:
          "Nothing at this address belongs to your merchant, or it does not exist. Cross merchant identifiers are indistinguishable from unknown ones.",
      },
    };
  }

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    return {
      ok: false,
      failure: {
        reason: "apiError",
        status: response.status,
        message: `The AgentRank API answered HTTP ${String(response.status)}.`,
      },
    };
  }

  try {
    return { ok: true, data: decode(payload) };
  } catch (error) {
    const detail = error instanceof DecodeError ? error.message : "unexpected response shape";
    return {
      ok: false,
      failure: {
        reason: "invalidResponse",
        message: `The API response was not readable: ${detail}`,
      },
    };
  }
}
