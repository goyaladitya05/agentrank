"use server";

/**
 * Sign in and sign out for the console.
 *
 * Signing in is one exchange: the pasted merchant API key opens a durable session on the
 * AgentRank API, and the key is then done with. It is not stored, not cached and not carried
 * into any later request, so a console process holds no merchant credential at any point after
 * this function returns. Verification is not a separate probe any more; opening the session is
 * the verification, and a key the API will not accept opens nothing.
 *
 * The browser receives a random cookie value, and the credential the API knows about is derived
 * from it rather than equal to it. See `@/lib/auth/session`.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import {
  SESSION_COOKIE_NAMES,
  newCookieValue,
  presentedCookie,
  sessionCookieName,
  sessionCookieOptions,
  sessionVerifier,
} from "@/lib/auth/session";
import { apiBaseUrl } from "@/lib/config";

export type SignInError = "empty" | "rejected" | "unreachable" | "unusable";

const SESSIONS = "/api/v1/console/sessions";

export async function signIn(formData: FormData): Promise<void> {
  const apiKey = String(formData.get("apiKey") ?? "").trim();
  if (apiKey.length === 0) {
    redirect("/login?error=empty");
  }

  const cookieValue = newCookieValue();
  // Derived before the request, so a console with no session secret configured fails here
  // rather than after a session has been opened that it could never resolve again.
  const verifier = sessionVerifier(cookieValue);
  if (verifier === null) {
    redirect("/login?error=unusable");
  }

  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl().replace(/\/+$/, "")}${SESSIONS}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ verifier }),
      cache: "no-store",
    });
  } catch {
    redirect("/login?error=unreachable");
  }

  if (response.status === 401 || response.status === 403) {
    redirect("/login?error=rejected");
  }
  if (!response.ok) {
    redirect("/login?error=unreachable");
  }

  // The lifetime the API measured, not one this process computes. Both halves of that
  // subtraction are the database's clock, so a console running fast cannot hand a merchant a
  // cookie that expires before their session does, or compute zero and make signing in
  // impossible while telling them to check the API they are pointing at.
  let lifetime: unknown = null;
  try {
    lifetime = ((await response.json()) as { expires_in_seconds?: unknown }).expires_in_seconds;
  } catch {
    redirect("/login?error=unusable");
  }
  if (typeof lifetime !== "number" || !Number.isFinite(lifetime) || lifetime <= 0) {
    redirect("/login?error=unusable");
  }

  const jar = await cookies();
  jar.set(sessionCookieName(), cookieValue, sessionCookieOptions(lifetime));
  redirect("/overview");
}

export async function signOut(): Promise<void> {
  const jar = await cookies();
  const verifier = sessionVerifier(presentedCookie(jar));
  // Both names, because a deployment that gained or lost HTTPS since this browser signed in may
  // hold the other one, and a sign out that left it behind would be a sign out that did not.
  //
  // The cookie goes whatever the API says. A browser that keeps presenting a session the server
  // has closed learns nothing useful, and a network failure here must not leave somebody looking
  // at a console they asked to leave.
  for (const name of SESSION_COOKIE_NAMES) {
    jar.delete(name);
  }
  if (verifier !== null) {
    try {
      await fetch(`${apiBaseUrl().replace(/\/+$/, "")}${SESSIONS}/current`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${verifier}` },
        cache: "no-store",
      });
    } catch {
      // Revocation is best effort from here and the session expires on its own regardless. The
      // failure is not shown: it says nothing the merchant can act on, and signing out has
      // already happened as far as this browser is concerned.
    }
  }
  redirect("/login");
}
