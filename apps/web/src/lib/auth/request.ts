import { cookies } from "next/headers";

import { SESSION_COOKIE, sessionVerifier } from "./session";

/** Refuse cross-site unsafe requests before a merchant credential is used. */
export async function mutationCredential(request: Request): Promise<string | null> {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (origin === null || host === null) {
    return null;
  }
  let originHost: string;
  try {
    originHost = new URL(origin).host;
  } catch {
    return null;
  }
  if (originHost !== host) {
    return null;
  }
  const jar = await cookies();
  return sessionVerifier(jar.get(SESSION_COOKIE)?.value);
}

/** Read-only server proxy calls still require an explicit browser session. */
export async function requestCredential(): Promise<string | null> {
  const jar = await cookies();
  return sessionVerifier(jar.get(SESSION_COOKIE)?.value);
}
