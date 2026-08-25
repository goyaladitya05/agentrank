/**
 * What this console process does before it serves its first request.
 *
 * Next.js calls `register` once per server process at startup, which is the only hook this
 * console has for the thing Phase 5A wanted: a required setting that is missing should stop the
 * process here, with the variable named, rather than surfacing as a failed sign in later.
 *
 * Nothing else belongs here. No database connection, no call to the API, no warm-up. A console
 * that refused to start because the API was briefly down would be a console that cannot come
 * back up during the outage it is meant to help diagnose; whether its dependencies are reachable
 * is a readiness question and is answered at `/api/ready`.
 */

import { inspectConfiguration, requireUsableConfiguration } from "@/lib/configuration";

export function register(): void {
  requireUsableConfiguration();

  // One line, and only about settings that are not the ordinary production shape. Variable names
  // and never values: an operator reading a boot log should be able to see that the insecure
  // cookie exception is on without the log becoming somewhere secrets accumulate.
  const report = inspectConfiguration();
  for (const relaxation of report.relaxed) {
    console.warn(`agentrank console: ${relaxation}`);
  }
  if (report.usingDefaultApiBaseUrl) {
    console.warn(
      "agentrank console: AGENTRANK_API_BASE_URL is not set, using the localhost default.",
    );
  }
}
