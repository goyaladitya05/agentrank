/**
 * What this console process needs before it can serve anything, checked in one place.
 *
 * The console used to need almost nothing: a base URL with a working default, and a session
 * store that was a map in memory. Neither could be misconfigured because neither was configured.
 * A durable session changes that, so there is now exactly one setting a console cannot start
 * without, and it fails at startup naming it rather than at the first sign in.
 *
 * The distinction this module draws is the one an operator has to act on:
 *
 * ```text
 * required   the process cannot do its job without it, so it refuses to start
 * defaulted  absent means a documented default, which is a working deployment
 * relaxed    a production safety rule this deployment has explicitly turned off
 * ```
 *
 * Nothing here reads a value into a message. What a failure names is the variable, which is not
 * a secret, and nothing logs, echoes or returns what was configured.
 *
 * The `.env` rule mirrors the backend's and exists for the same reason, with one difference that
 * changes what can be done about it. `agentrank_api.config` chooses whether to read the file, so
 * a deployment simply does not. Next.js reads `.env` itself, before any code here runs, so by the
 * time this is asked the file has already contributed. The only honest protection left is to
 * refuse to serve at all when a deployment has one, which is what happens: a `.env` shipped into
 * a deployment is a file that may already have decided something, and a console that started
 * anyway would be a console nobody could reason about.
 */

import { existsSync } from "node:fs";

import {
  COOKIE_SECURE_VARIABLE,
  MIN_SESSION_SECRET_LENGTH,
  SESSION_SECRET_VARIABLE,
  cookiesAreSecure,
  sessionSecret,
} from "@/lib/auth/session";

export { COOKIE_SECURE_VARIABLE };
import { API_BASE_URL_VARIABLE, DEFAULT_API_BASE_URL, apiBaseUrl } from "@/lib/config";

export interface ConfigurationReport {
  /** Variables that are required and are missing or unusable, named without their values. */
  readonly problems: readonly string[];
  /** Production safety rules this deployment has explicitly relaxed. */
  readonly relaxed: readonly string[];
  /** Whether the API base URL is this console's default rather than a configured one. */
  readonly usingDefaultApiBaseUrl: boolean;
}

export const ENVIRONMENT_VARIABLE = "AGENTRANK_ENV";

export const ENV_FILE = ".env";

/**
 * The environments that may be configured from a file on disk.
 *
 * The same three `agentrank_api.config.FILE_CONFIGURED_ENVIRONMENTS` names. Two copies of a set
 * of three strings, because the alternative is the console importing from the backend package,
 * and a console that could not start without the Python distribution installed beside it would be
 * a worse coupling than this one.
 */
export const FILE_CONFIGURED_ENVIRONMENTS = new Set(["development", "ci", "test"]);

/** Whether this process is one that may be configured from a file. */
export function fileConfigured(): boolean {
  const environment = (process.env[ENVIRONMENT_VARIABLE] ?? "development").trim();
  return FILE_CONFIGURED_ENVIRONMENTS.has(environment === "" ? "development" : environment);
}

/**
 * Everything wrong with this process' configuration, gathered rather than thrown one at a time.
 *
 * All of it at once, because an operator fixing a deployment should learn about the second
 * missing variable from this message and not from the next failed start.
 */
export function inspectConfiguration(): ConfigurationReport {
  const problems: string[] = [];
  const relaxed: string[] = [];

  try {
    sessionSecret();
  } catch {
    // The message is rebuilt here rather than forwarded, so there is no path by which a value
    // could reach this report through an error somebody later decides to include verbatim.
    problems.push(
      `${SESSION_SECRET_VARIABLE} is missing or shorter than ${String(MIN_SESSION_SECRET_LENGTH)} characters. Browser sessions are derived from it and cannot be issued without one.`,
    );
  }

  const configuredBaseUrl = process.env[API_BASE_URL_VARIABLE];
  if (configuredBaseUrl !== undefined && configuredBaseUrl.length > 0) {
    try {
      const parsed = new URL(configuredBaseUrl);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        problems.push(`${API_BASE_URL_VARIABLE} must be an http or https URL.`);
      }
    } catch {
      problems.push(`${API_BASE_URL_VARIABLE} is not a URL.`);
    }
  }

  if (!fileConfigured() && existsSync(ENV_FILE)) {
    problems.push(
      `${ENV_FILE} is present and ${ENVIRONMENT_VARIABLE} names a deployment. Next.js reads that file before this console runs, so a deployment must not ship one: configure this process through its environment and remove the file.`,
    );
  }

  // Asked of the function that decides it rather than of the variable, because there are two
  // ways to turn it off: the documented variable, and `NODE_ENV=development`. Reporting only the
  // first meant a console started with the second issued cookies without `Secure` while its boot
  // log and its readiness both said nothing was relaxed.
  if (!cookiesAreSecure()) {
    relaxed.push(
      `session cookies are not marked Secure and are sent over plain HTTP, and the __Host- cookie prefix is unavailable. Set ${COOKIE_SECURE_VARIABLE} unset and NODE_ENV to something other than development outside local development.`,
    );
  }

  return {
    problems,
    relaxed,
    usingDefaultApiBaseUrl: apiBaseUrl() === DEFAULT_API_BASE_URL,
  };
}

/**
 * Refuse to continue when a required setting is missing.
 *
 * Called from `instrumentation.ts`, so a console with no session secret dies at boot with the
 * variable named. The alternative is a console that starts, renders a sign in page, and fails
 * on the one request that matters with an error nobody can place.
 */
export function requireUsableConfiguration(): void {
  const report = inspectConfiguration();
  if (report.problems.length > 0) {
    throw new Error(
      `The AgentRank console cannot start:\n${report.problems.map((problem) => `  - ${problem}`).join("\n")}`,
    );
  }
}
