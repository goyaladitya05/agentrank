/**
 * The base URL is read on the server only. The console fetches the API from the Next.js
 * server rather than the browser, so the value is not exposed to the client and no CORS
 * configuration is needed.
 */
export const DEFAULT_API_BASE_URL = "http://localhost:8000";

export const API_BASE_URL_VARIABLE = "AGENTRANK_API_BASE_URL";

export function apiBaseUrl(): string {
  return process.env[API_BASE_URL_VARIABLE] ?? DEFAULT_API_BASE_URL;
}
