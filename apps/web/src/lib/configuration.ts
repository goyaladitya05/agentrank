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
 */

import {
  MIN_SESSION_SECRET_LENGTH,
  SESSION_SECRET_VARIABLE,
  sessionSecret,
} from "@/lib/auth/session";
import { API_BASE_URL_VARIABLE, DEFAULT_API_BASE_URL, apiBaseUrl } from "@/lib/config";

export interface ConfigurationReport {
  /** Variables that are required and are missing or unusable, named without their values. */
  readonly problems: readonly string[];
  /** Production safety rules this deployment has explicitly relaxed. */
  readonly relaxed: readonly string[];
  /** Whether the API base URL is this console's default rather than a configured one. */
  readonly usingDefaultApiBaseUrl: boolean;
}

export const COOKIE_SECURE_VARIABLE = "AGENTRANK_COOKIE_SECURE";

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

  if (process.env[COOKIE_SECURE_VARIABLE] === "false") {
    relaxed.push(
      `${COOKIE_SECURE_VARIABLE}=false: session cookies are sent over plain HTTP. Local development only.`,
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
