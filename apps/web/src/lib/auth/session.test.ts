import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  COOKIE_SCHEME,
  MIN_SESSION_SECRET_LENGTH,
  SESSION_SECRET_VARIABLE,
  VERIFIER_SCHEME,
  newCookieValue,
  sessionCookieOptions,
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
