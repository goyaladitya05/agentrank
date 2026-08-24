/**
 * Resolving the merchant credential a server render should call the API with.
 *
 * An explicitly signed in session wins over the server environment credential, so an
 * operator can inspect another merchant without redeploying. Both stay on the server:
 * nothing here is importable from client code.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { resolveApiKeyForToken, SESSION_COOKIE } from "@/lib/auth/session";

export function environmentApiKey(): string | null {
  const token = process.env.AGENTRANK_MERCHANT_API_KEY;
  return token !== undefined && token.length > 0 ? token : null;
}

export async function consoleApiKey(): Promise<string | null> {
  const jar = await cookies();
  return resolveApiKeyForToken(jar.get(SESSION_COOKIE)?.value);
}

/**
 * The credential every product page starts from. Without one there is nothing to show
 * except the sign in page, so the redirect is the behavior rather than an error state.
 */
export async function requireConsoleApiKey(): Promise<string> {
  const apiKey = await consoleApiKey();
  if (apiKey === null) {
    redirect("/login");
  }
  return apiKey;
}
