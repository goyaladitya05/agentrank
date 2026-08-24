/**
 * Loading an insight for a page render.
 *
 * Every product route goes through this one seam: the credential requirement, the API
 * base URL and the failure classification are decided once, and pages only decide how to
 * render the answer.
 */

import { apiBaseUrl } from "@/lib/config";
import { requireConsoleApiKey } from "@/lib/auth/credential";
import { fetchInsight, type InsightsFailure, type InsightsOutcome } from "./client";

export type { InsightsFailure };

export async function loadInsight<T>(
  path: string,
  decode: (value: unknown) => T,
): Promise<InsightsOutcome<T>> {
  const apiKey = await requireConsoleApiKey();
  return fetchInsight(path, decode, { baseUrl: apiBaseUrl(), apiKey });
}
