/**
 * The console's merchant session.
 *
 * The session used to live in this process' memory: a random cookie token, a map from that token
 * to the merchant API key, and a deployment story that ended at localhost. A cookie identified a
 * session only to the process that minted it, so a second console instance signed the merchant
 * out and so did every restart. The session record now lives in PostgreSQL behind the AgentRank
 * API, and this module holds the browser half of it.
 *
 * Two values, and keeping them apart is the whole design.
 *
 * The **cookie value** is what the browser holds: `arc_` and 256 random bits, httpOnly, so client
 * JavaScript never sees it and it never enters a URL or persistent storage.
 *
 * The **session verifier** is what this server presents to the API: `ars_` and the HMAC of the
 * cookie value under a secret only this deployment holds. That is the credential the API knows
 * about; the cookie is not. A cookie recovered from a retained browser trace, a proxy log or a
 * support screenshot is inert without `AGENTRANK_CONSOLE_SESSION_SECRET`, and the secret is inert
 * without a cookie. Neither half is sufficient.
 *
 * The merchant API key is not here at all. It is typed once into the sign in form, presented once
 * to open a session, and forgotten. This process stores no merchant credential, in memory or
 * anywhere else, so there is nothing for a second console instance to be missing and nothing for
 * a memory dump to contain.
 *
 * Derivation is deterministic and stateless, so every console process derives the same verifier
 * from the same cookie. That is what lets any instance serve any request and what lets a restart
 * keep every session it had.
 */

import { createHmac, randomBytes } from "node:crypto";

export const SESSION_COOKIE = "ar_console_session";

/** What the browser holds. Recognisable, and deliberately not what the API is told about. */
export const COOKIE_SCHEME = "arc";

/** What this server presents to the API. Kept in step with `agentrank_api.auth.console`. */
export const VERIFIER_SCHEME = "ars";

/** 256 bits, hex encoded, for both halves. */
const SECRET_BYTES = 32;
const HEX_LENGTH = SECRET_BYTES * 2;

const COOKIE_PATTERN = new RegExp(`^${COOKIE_SCHEME}_[0-9a-f]{${HEX_LENGTH}}$`);

/**
 * The shortest deployment secret this console will start with.
 *
 * An HMAC key shorter than its digest adds no strength, and a short one here is almost always a
 * placeholder somebody meant to replace. Refusing it at startup is cheaper than discovering it
 * in an incident.
 */
export const MIN_SESSION_SECRET_LENGTH = 32;

export const SESSION_SECRET_VARIABLE = "AGENTRANK_CONSOLE_SESSION_SECRET";

/**
 * The deployment secret the verifier is derived under, or an error naming what is missing.
 *
 * Read on every derivation rather than captured at import. A module level constant would be
 * read once at build time in some Next.js contexts, and a console that baked a development
 * secret into a production bundle is exactly the failure this whole scheme exists to avoid.
 * The read is a property access; the HMAC beside it dominates the cost either way.
 *
 * The value is never returned to a caller that renders, never logged and never included in an
 * error message. What the message names is the variable, which is not a secret.
 */
export function sessionSecret(): string {
  const configured = process.env[SESSION_SECRET_VARIABLE];
  if (configured === undefined || configured.trim().length === 0) {
    throw new Error(
      `${SESSION_SECRET_VARIABLE} is required. The console derives every browser session credential from it, and without one no session it issues could be resolved after a restart.`,
    );
  }
  if (configured.length < MIN_SESSION_SECRET_LENGTH) {
    throw new Error(
      `${SESSION_SECRET_VARIABLE} must be at least ${String(MIN_SESSION_SECRET_LENGTH)} characters.`,
    );
  }
  return configured;
}

/** A fresh cookie value. Nothing derives it from the merchant, the request or the clock. */
export function newCookieValue(): string {
  return `${COOKIE_SCHEME}_${randomBytes(SECRET_BYTES).toString("hex")}`;
}

/**
 * The session verifier one cookie value stands for, or null when there is no usable cookie.
 *
 * The shape is checked before the HMAC, so a cookie somebody edited by hand, a value from an
 * older console and an empty string all answer the same way: null, and no credential is derived
 * from any of them. A null answer is "sign in", never a request sent without a credential.
 */
export function sessionVerifier(cookieValue: string | undefined | null): string | null {
  if (cookieValue === undefined || cookieValue === null || !COOKIE_PATTERN.test(cookieValue)) {
    return null;
  }
  const digest = createHmac("sha256", sessionSecret()).update(cookieValue).digest("hex");
  return `${VERIFIER_SCHEME}_${digest}`;
}

/**
 * Cookie attributes for the session. Production is always secure. Local HTTP development is the
 * only explicit exception, controlled by AGENTRANK_COOKIE_SECURE=false.
 *
 * `maxAge` comes from the expiry the API reported rather than from a constant here. The API
 * decides how long a session lives, from the database clock, and a console that kept its own
 * idea of the lifetime would either drop a session that is still good or hold one the API has
 * already stopped honouring.
 */
export function sessionCookieOptions(maxAgeSeconds: number): {
  httpOnly: true;
  sameSite: "lax";
  path: "/";
  maxAge: number;
  secure: boolean;
} {
  return {
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: Math.max(0, Math.floor(maxAgeSeconds)),
    secure:
      process.env.AGENTRANK_COOKIE_SECURE !== "false" && process.env.NODE_ENV !== "development",
  };
}
