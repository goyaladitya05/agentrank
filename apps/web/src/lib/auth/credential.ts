/**
 * Resolving the credential a server render should call the API with.
 *
 * One source, and it is the browser's own session cookie. There is no environment merchant
 * credential and no fallback to one: a console process holding a merchant API key would be a
 * process that could answer for a merchant nobody signed in as, and every request this console
 * makes now carries a credential that belongs to one signed in browser.
 *
 * What is returned is a console session verifier, not a merchant API key. The key never reaches
 * this module: it is typed into the sign in form once, exchanged for a session, and forgotten.
 * Nothing here is importable from client code.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { SESSION_COOKIE, sessionVerifier } from "@/lib/auth/session";

export async function consoleCredential(): Promise<string | null> {
  const jar = await cookies();
  return sessionVerifier(jar.get(SESSION_COOKIE)?.value);
}

/**
 * The credential every product page starts from. Without one there is nothing to show except
 * the sign in page, so the redirect is the behavior rather than an error state.
 *
 * A cookie that is present is not a session that is still open. Whether this credential still
 * authenticates is the API's answer and not this console's, because the session record lives
 * there; an expired or revoked one comes back as an unauthenticated failure the page renders as
 * a prompt to sign in again. This checks only that a browser presented something worth asking
 * about, which is what keeps a signed out visitor from generating an API call at all.
 */
export async function requireConsoleCredential(): Promise<string> {
  const credential = await consoleCredential();
  if (credential === null) {
    redirect("/login");
  }
  return credential;
}
