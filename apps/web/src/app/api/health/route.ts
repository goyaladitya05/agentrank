/**
 * Liveness for the console process.
 *
 * Answers from the process alone. No database, no API call, no configuration read, so an
 * orchestrator can tell "this process is wedged" apart from "something it depends on is down".
 * A liveness probe that consulted a dependency would restart a healthy console during an outage
 * of something restarting it cannot fix.
 */

export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok" });
}
