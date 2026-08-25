/**
 * Loading an insight for a page render.
 *
 * Every product route goes through this one seam: the credential requirement, the API
 * base URL and the failure classification are decided once, and pages only decide how to
 * render the answer.
 */

import { redirect } from "next/navigation";

import { requireConsoleCredential } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { fetchInsight, type InsightsFailure, type InsightsOutcome } from "./client";

export type { InsightsFailure };

export async function loadInsight<T>(
  path: string,
  decode: (value: unknown) => T,
): Promise<InsightsOutcome<T>> {
  const credential = await requireConsoleCredential();
  const outcome = await fetchInsight(path, decode, { baseUrl: apiBaseUrl(), credential });
  // A session the API will not accept is a session that has ended, and the recovery from that is
  // signing in rather than a page full of error panels the merchant cannot act on. It is decided
  // here because a cookie being present is no longer the same question as a session being open:
  // the record lives in the API, so an expired one, a revoked one and one whose merchant
  // credential was withdrawn all look identical to this console until it asks.
  //
  // The cookie is left in place. A render cannot write one in Next.js, and a stale cookie is
  // inert anyway: signing in replaces it, and until then every attempt lands here again.
  if (!outcome.ok && outcome.failure.reason === "unauthenticated") {
    redirect("/login?error=expired");
  }
  return outcome;
}
