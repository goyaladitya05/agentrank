import { cookies } from "next/headers";

import { presentedCookie, sessionVerifier } from "./session";

/**
 * Refuse cross-site unsafe requests before a merchant credential is used.
 *
 * `X-Forwarded-Host` is preferred over `Host` when present, which is what Next.js' own Server
 * Action origin check does and is required by the topology this console is deployed under. A
 * TLS-terminating proxy in front of two console instances forwards `Host: <instance>:3000` and
 * `X-Forwarded-Host: console.example.com`, so comparing `Origin` against the raw `Host` would
 * fail for every honest request and these two write paths would answer 401 while every server
 * action kept working.
 *
 * That header is caller-supplied and therefore trusted only as far as the proxy in front of this
 * process is: a deployment that exposes the console directly, with nothing stripping it, lets a
 * cross-site caller state both halves of this comparison. The same is true of the framework's own
 * check, so this is the deployment assumption Next.js already makes rather than a new one, and
 * the console is not the thing enforcing tenancy in any case: every request it forwards carries a
 * session credential the API resolves to one merchant.
 */
export async function mutationCredential(request: Request): Promise<string | null> {
  const origin = request.headers.get("origin");
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
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
  return sessionVerifier(presentedCookie(jar));
}

/** Read-only server proxy calls still require an explicit browser session. */
export async function requestCredential(): Promise<string | null> {
  const jar = await cookies();
  return sessionVerifier(presentedCookie(jar));
}
