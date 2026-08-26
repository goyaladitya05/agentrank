import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  COOKIE_SCHEME,
  COOKIE_SECURE_VARIABLE,
  MIN_SESSION_SECRET_LENGTH,
  SECURE_SESSION_COOKIE,
  SESSION_COOKIE,
  SESSION_SECRET_VARIABLE,
  VERIFIER_SCHEME,
  cookiesAreSecure,
  newCookieValue,
  presentedCookie,
  sessionCookieName,
  sessionCookieOptions,
  sessionCookieRemoval,
  sessionSecret,
  sessionVerifier,
} from "./session";

/**
 * The browser half of the console session.
 *
 * What is worth asserting here is that the cookie and the credential the API knows about are two
 * different values, that the derivation between them is deterministic across processes, and that
 * a console with no deployment secret refuses to derive one at all rather than falling back to
 * something weaker.
 */

const SECRET = "a-console-session-secret-of-sufficient-length";
const OTHER_SECRET = "a-different-console-session-secret-entirely";

let original: string | undefined;

beforeEach(() => {
  original = process.env[SESSION_SECRET_VARIABLE];
  process.env[SESSION_SECRET_VARIABLE] = SECRET;
});

afterEach(() => {
  if (original === undefined) {
    delete process.env[SESSION_SECRET_VARIABLE];
  } else {
    process.env[SESSION_SECRET_VARIABLE] = original;
  }
});

describe("console session cookies", () => {
  it("mints a cookie value that is not the credential the API is told about", () => {
    const cookie = newCookieValue();
    const verifier = sessionVerifier(cookie);

    expect(cookie.startsWith(`${COOKIE_SCHEME}_`)).toBe(true);
    expect(verifier).not.toBeNull();
    expect(verifier?.startsWith(`${VERIFIER_SCHEME}_`)).toBe(true);
    // The one property the whole scheme exists for: holding the cookie is not holding the
    // credential, and the cookie does not appear inside it.
    expect(verifier).not.toBe(cookie);
    expect(verifier).not.toContain(cookie.slice(COOKIE_SCHEME.length + 1));
  });

  it("mints a different cookie value every time", () => {
    expect(newCookieValue()).not.toBe(newCookieValue());
  });

  it("derives the same credential from the same cookie, which is what lets any process serve it", () => {
    const cookie = newCookieValue();
    expect(sessionVerifier(cookie)).toBe(sessionVerifier(cookie));
  });

  it("derives a different credential under a different deployment secret", () => {
    const cookie = newCookieValue();
    const here = sessionVerifier(cookie);
    process.env[SESSION_SECRET_VARIABLE] = OTHER_SECRET;
    expect(sessionVerifier(cookie)).not.toBe(here);
  });

  it("answers null for a missing, empty or malformed cookie rather than deriving anything", () => {
    expect(sessionVerifier(undefined)).toBeNull();
    expect(sessionVerifier(null)).toBeNull();
    expect(sessionVerifier("")).toBeNull();
    expect(sessionVerifier("not-a-cookie")).toBeNull();
    expect(sessionVerifier(`${COOKIE_SCHEME}_short`)).toBeNull();
    // Uppercase hex is not the shape this console mints, so it is not one of ours.
    expect(sessionVerifier(`${COOKIE_SCHEME}_${"A1".repeat(32)}`)).toBeNull();
    // A merchant API key pasted into the cookie is not a session either.
    expect(sessionVerifier(`ar_dev_${"a".repeat(32)}_${"b".repeat(64)}`)).toBeNull();
  });

  it("refuses to derive a credential when the deployment has no session secret", () => {
    delete process.env[SESSION_SECRET_VARIABLE];
    expect(() => sessionVerifier(newCookieValue())).toThrow(SESSION_SECRET_VARIABLE);
  });

  it("refuses a session secret too short to be one", () => {
    process.env[SESSION_SECRET_VARIABLE] = "x".repeat(MIN_SESSION_SECRET_LENGTH - 1);
    expect(() => sessionSecret()).toThrow(SESSION_SECRET_VARIABLE);
  });

  it("never puts the deployment secret into the error that names it", () => {
    process.env[SESSION_SECRET_VARIABLE] = "x".repeat(MIN_SESSION_SECRET_LENGTH - 1);
    try {
      sessionSecret();
      expect.unreachable("a short secret must be refused");
    } catch (error) {
      expect(String(error)).not.toContain("x".repeat(MIN_SESSION_SECRET_LENGTH - 1));
    }
  });
});

describe("console session cookie attributes", () => {
  it("is httpOnly, same site and scoped to the whole console", () => {
    const options = sessionCookieOptions(3600);
    expect(options.httpOnly).toBe(true);
    expect(options.sameSite).toBe("lax");
    expect(options.path).toBe("/");
    expect(options.maxAge).toBe(3600);
  });

  it("takes its lifetime from the API rather than from a constant here", () => {
    expect(sessionCookieOptions(60).maxAge).toBe(60);
    expect(sessionCookieOptions(12 * 60 * 60).maxAge).toBe(12 * 60 * 60);
  });

  it("never sets a negative or fractional lifetime", () => {
    expect(sessionCookieOptions(-1).maxAge).toBe(0);
    expect(sessionCookieOptions(1.9).maxAge).toBe(1);
  });

  it("is secure unless local HTTP development explicitly says otherwise", () => {
    const before = process.env.AGENTRANK_COOKIE_SECURE;
    delete process.env.AGENTRANK_COOKIE_SECURE;
    expect(sessionCookieOptions(60).secure).toBe(process.env.NODE_ENV !== "development");
    process.env.AGENTRANK_COOKIE_SECURE = "false";
    expect(sessionCookieOptions(60).secure).toBe(false);
    if (before === undefined) {
      delete process.env.AGENTRANK_COOKIE_SECURE;
    } else {
      process.env.AGENTRANK_COOKIE_SECURE = before;
    }
  });
});

describe("which cookie name a session is written under and read from", () => {
  /**
   * `__Host-` is what stops a sibling subdomain, an extension or an XSS anywhere under the
   * registrable domain planting a path-scoped cookie that outranks the real one and quietly moves
   * a merchant into somebody else's tenant. It requires `Secure`, so plain HTTP localhost cannot
   * have it and gets the unprefixed name instead.
   */

  const savedSecure = process.env[COOKIE_SECURE_VARIABLE];

  afterEach(() => {
    if (savedSecure === undefined) {
      delete process.env[COOKIE_SECURE_VARIABLE];
    } else {
      process.env[COOKIE_SECURE_VARIABLE] = savedSecure;
    }
  });

  it("writes the prefixed name when the cookie can be Secure", () => {
    delete process.env[COOKIE_SECURE_VARIABLE];
    expect(cookiesAreSecure()).toBe(true);
    expect(sessionCookieName()).toBe(SECURE_SESSION_COOKIE);
    expect(sessionCookieOptions(60).secure).toBe(true);
  });

  it("falls back to the plain name only where Secure is explicitly off", () => {
    process.env[COOKIE_SECURE_VARIABLE] = "false";
    expect(sessionCookieName()).toBe(SESSION_COOKIE);
    expect(sessionCookieOptions(60).secure).toBe(false);
  });

  it("prefers the prefixed cookie when a request presents both", () => {
    const injected = `${COOKIE_SCHEME}_${"a".repeat(64)}`;
    const genuine = `${COOKIE_SCHEME}_${"b".repeat(64)}`;
    const jar = {
      get: (name: string) =>
        name === SECURE_SESSION_COOKIE
          ? { value: genuine }
          : name === SESSION_COOKIE
            ? { value: injected }
            : undefined,
    };
    // A prefixed cookie is one no other host and no other path could have set, so an injected
    // unprefixed one must not displace it by being read first.
    expect(presentedCookie(jar)).toBe(genuine);
  });

  it("refuses the plain name entirely on a deployment that can use the prefix", () => {
    delete process.env[COOKIE_SECURE_VARIABLE];
    // The attack the prefix exists to close. A sibling subdomain, an extension or an XSS on any
    // host under the registrable domain can plant `ar_console_session=<value>; Path=/overview`,
    // which sorts before the real cookie by path length and cannot be cleared by anything
    // writing `Path=/`. Preferring the prefixed name is not enough, because a victim who has
    // signed out has no prefixed cookie for it to be preferred over.
    const planted = `${COOKIE_SCHEME}_${"c".repeat(64)}`;
    const jar = {
      get: (name: string) => (name === SESSION_COOKIE ? { value: planted } : undefined),
    };

    expect(presentedCookie(jar)).toBeUndefined();
  });

  it("reads the plain name on a deployment that cannot set Secure", () => {
    // Local HTTP development, which is the one shape `AGENTRANK_COOKIE_SECURE=false` exists for
    // and the one where a `__Host-` cookie cannot be written at all.
    process.env[COOKIE_SECURE_VARIABLE] = "false";
    const value = `${COOKIE_SCHEME}_${"c".repeat(64)}`;
    const jar = { get: (name: string) => (name === SESSION_COOKIE ? { value } : undefined) };

    expect(presentedCookie(jar)).toBe(value);
  });

  it("answers undefined when no session cookie is present at all", () => {
    expect(presentedCookie({ get: () => undefined })).toBeUndefined();
    expect(presentedCookie({ get: () => ({ value: "" }) })).toBeUndefined();
  });
});

describe("removing a session cookie", () => {
  it("carries the attributes the browser needs to accept the removal", () => {
    // A `__Host-` prefixed cookie may only be written with Secure and Path=/. A removal without
    // them is refused by the browser and the session cookie survives a sign out.
    const removal = sessionCookieRemoval(SECURE_SESSION_COOKIE);

    expect(removal.secure).toBe(true);
    expect(removal.path).toBe("/");
    expect(removal.maxAge).toBe(0);
    expect(removal.httpOnly).toBe(true);
  });

  it("removes the unprefixed name at the root path rather than at the current one", () => {
    // Without Path=/ the removal takes the request's default path, so signing out from a nested
    // route left the cookie in place for every other route.
    expect(sessionCookieRemoval(SESSION_COOKIE).path).toBe("/");
  });
});
