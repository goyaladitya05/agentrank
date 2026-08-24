/**
 * The console's merchant session.
 *
 * The narrowest safe session mechanism for a single tenant operations console: the
 * merchant pastes their API key once, the Next.js server verifies it against the real
 * API, and the key then lives only in this process' memory. The browser holds a random
 * session token in one httpOnly cookie and nothing else; the key itself never reaches
 * client JavaScript, a URL, or persistent storage.
 *
 * Sessions are deliberately in-process. They survive hot reloads through globalThis and
 * die with the server, which is honest for a console whose deployment story is still
 * localhost. A multi instance deployment needs a real session store first, and that is
 * recorded as a shortcoming rather than papered over here.
 */

import { randomBytes } from "node:crypto";

export const SESSION_COOKIE = "ar_console_session";

/** Twelve hours of idle usefulness, matching a working day rather than forever. */
export const SESSION_TTL_MS = 12 * 60 * 60 * 1000;
export const SESSION_TTL_SECONDS = SESSION_TTL_MS / 1000;

const MAX_SESSIONS = 200;

interface SessionEntry {
  readonly apiKey: string;
  readonly expiresAtMs: number;
}

interface SessionGlobal {
  __agentrankConsoleSessions?: Map<string, SessionEntry>;
}

function store(): Map<string, SessionEntry> {
  const globalObject = globalThis as SessionGlobal;
  globalObject.__agentrankConsoleSessions ??= new Map<string, SessionEntry>();
  return globalObject.__agentrankConsoleSessions;
}

function pruneExpired(nowMs: number): void {
  const sessions = store();
  for (const [token, entry] of sessions) {
    if (entry.expiresAtMs <= nowMs) {
      sessions.delete(token);
    }
  }
}

export function createSession(apiKey: string, nowMs: number = Date.now()): string {
  const sessions = store();
  pruneExpired(nowMs);
  while (sessions.size >= MAX_SESSIONS) {
    const oldest = sessions.keys().next().value;
    if (oldest === undefined) {
      break;
    }
    sessions.delete(oldest);
  }
  const token = randomBytes(32).toString("hex");
  sessions.set(token, { apiKey, expiresAtMs: nowMs + SESSION_TTL_MS });
  return token;
}

export function resolveApiKeyForToken(
  token: string | undefined | null,
  nowMs: number = Date.now(),
): string | null {
  if (token === undefined || token === null || token.length === 0) {
    return null;
  }
  const entry = store().get(token);
  if (entry === undefined) {
    return null;
  }
  if (entry.expiresAtMs <= nowMs) {
    store().delete(token);
    return null;
  }
  return entry.apiKey;
}

export function destroySession(token: string | undefined | null): void {
  if (token === undefined || token === null || token.length === 0) {
    return;
  }
  store().delete(token);
}

export function sessionCount(): number {
  return store().size;
}

/**
 * Cookie attributes for the session token. Production is always secure. Local HTTP development
 * is the only explicit exception, controlled by AGENTRANK_COOKIE_SECURE=false.
 */
export function sessionCookieOptions(): {
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
    maxAge: SESSION_TTL_SECONDS,
    secure:
      process.env.AGENTRANK_COOKIE_SECURE !== "false" && process.env.NODE_ENV !== "development",
  };
}
