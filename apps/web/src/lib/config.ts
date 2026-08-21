/**
 * The base URL is read on the server only. The console fetches the API from the Next.js
 * server rather than the browser, so the value is not exposed to the client and no CORS
 * configuration is needed.
 */
const DEFAULT_API_BASE_URL = "http://localhost:8000";

export function apiBaseUrl(): string {
  return process.env.AGENTRANK_API_BASE_URL ?? DEFAULT_API_BASE_URL;
}
