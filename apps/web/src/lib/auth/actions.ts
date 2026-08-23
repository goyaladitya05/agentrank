"use server";

/**
 * Sign in and sign out for the console.
 *
 * Signing in verifies the pasted key against the real insights API before any session is
 * created, so a typo becomes an error message rather than a broken console. The key is
 * then stored in the server side session vault only; the cookie carries a random token.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import {
  createSession,
  destroySession,
  SESSION_COOKIE,
  sessionCookieOptions,
} from "@/lib/auth/session";
import { fetchInsight } from "@/lib/insights/client";
import { decodeRunSummaryList } from "@/lib/insights/decode";
import { apiBaseUrl } from "@/lib/config";

export type SignInError = "empty" | "rejected" | "unreachable" | "unusable";

export async function signIn(formData: FormData): Promise<void> {
  const apiKey = String(formData.get("apiKey") ?? "").trim();
  if (apiKey.length === 0) {
    redirect("/login?error=empty");
  }

  const probe = await fetchInsight("/api/v1/insights/runs?limit=1", decodeRunSummaryList, {
    baseUrl: apiBaseUrl(),
    apiKey,
  });

  if (!probe.ok) {
    const byReason: Record<string, SignInError> = {
      unauthenticated: "rejected",
      forbidden: "rejected",
      networkError: "unreachable",
      apiError: "unreachable",
      invalidResponse: "unusable",
      notFound: "unusable",
    };
    redirect(`/login?error=${byReason[probe.failure.reason] ?? "unreachable"}`);
  }

  const token = createSession(apiKey);
  const jar = await cookies();
  jar.set(SESSION_COOKIE, token, sessionCookieOptions());
  redirect("/overview");
}

export async function signOut(): Promise<void> {
  const jar = await cookies();
  destroySession(jar.get(SESSION_COOKIE)?.value);
  jar.delete(SESSION_COOKIE);
  redirect("/login");
}
