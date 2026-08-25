import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SESSION_SECRET_VARIABLE } from "@/lib/auth/session";
import { API_BASE_URL_VARIABLE } from "@/lib/config";
import {
  COOKIE_SECURE_VARIABLE,
  ENV_FILE,
  ENVIRONMENT_VARIABLE,
  fileConfigured,
  inspectConfiguration,
  requireUsableConfiguration,
} from "@/lib/configuration";

/**
 * What a console refuses to start with, and what it merely reports.
 *
 * The one rule worth guarding by test rather than by reading: a configuration failure names the
 * variable and never the value. An operator has to be able to paste a startup failure into a
 * ticket.
 */

const SECRET = "a-console-session-secret-of-sufficient-length";
const VARIABLES = [
  SESSION_SECRET_VARIABLE,
  API_BASE_URL_VARIABLE,
  COOKIE_SECURE_VARIABLE,
  ENVIRONMENT_VARIABLE,
];

const saved = new Map<string, string | undefined>();

beforeEach(() => {
  for (const name of VARIABLES) {
    saved.set(name, process.env[name]);
    delete process.env[name];
  }
  process.env[SESSION_SECRET_VARIABLE] = SECRET;
});

afterEach(() => {
  for (const name of VARIABLES) {
    const before = saved.get(name);
    if (before === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = before;
    }
  }
});

describe("console startup configuration", () => {
  it("starts with only a session secret set, and says the base URL is defaulted", () => {
    const report = inspectConfiguration();
    expect(report.problems).toEqual([]);
    expect(report.usingDefaultApiBaseUrl).toBe(true);
    expect(() => {
      requireUsableConfiguration();
    }).not.toThrow();
  });

  it("refuses to start with no session secret, naming the variable", () => {
    delete process.env[SESSION_SECRET_VARIABLE];
    expect(() => {
      requireUsableConfiguration();
    }).toThrow(SESSION_SECRET_VARIABLE);
  });

  it("refuses to start with a session secret too short to be one", () => {
    process.env[SESSION_SECRET_VARIABLE] = "too-short";
    expect(inspectConfiguration().problems).toHaveLength(1);
  });

  it("never puts a configured value into a problem it reports", () => {
    process.env[SESSION_SECRET_VARIABLE] = "too-short";
    process.env[API_BASE_URL_VARIABLE] = "not a url at all";
    const report = inspectConfiguration();
    expect(report.problems).toHaveLength(2);
    for (const problem of report.problems) {
      expect(problem).not.toContain("too-short");
      expect(problem).not.toContain("not a url at all");
    }
  });

  it("reports every problem at once rather than the first one", () => {
    delete process.env[SESSION_SECRET_VARIABLE];
    process.env[API_BASE_URL_VARIABLE] = "ftp://elsewhere.example";
    let message = "";
    try {
      requireUsableConfiguration();
    } catch (error) {
      message = String(error);
    }
    expect(message).toContain(SESSION_SECRET_VARIABLE);
    expect(message).toContain(API_BASE_URL_VARIABLE);
  });

  it("accepts a configured http or https base URL and refuses another scheme", () => {
    process.env[API_BASE_URL_VARIABLE] = "https://api.example";
    expect(inspectConfiguration().problems).toEqual([]);
    expect(inspectConfiguration().usingDefaultApiBaseUrl).toBe(false);
    process.env[API_BASE_URL_VARIABLE] = "file:///etc/passwd";
    expect(inspectConfiguration().problems).toHaveLength(1);
  });

  it("reports the insecure cookie exception without treating it as a failure", () => {
    process.env[COOKIE_SECURE_VARIABLE] = "false";
    const report = inspectConfiguration();
    expect(report.problems).toEqual([]);
    expect(report.relaxed).toHaveLength(1);
    expect(report.relaxed[0]).toContain(COOKIE_SECURE_VARIABLE);
  });

  it("treats any value other than the exact opt out as secure", () => {
    process.env[COOKIE_SECURE_VARIABLE] = "no";
    expect(inspectConfiguration().relaxed).toEqual([]);
  });

  it("reports the development relaxation too, which is the one that had no variable", () => {
    // There are two ways the cookie stops being Secure and only one of them is a variable
    // somebody set on purpose. A console started with NODE_ENV=development in a deployment used
    // to issue cookies without Secure while its boot log and readiness both reported nothing.
    // `vi.stubEnv` rather than an assignment: NODE_ENV is typed read-only, and redefining the
    // property is refused by Node on `process.env`.
    vi.stubEnv("NODE_ENV", "development");
    try {
      const report = inspectConfiguration();
      expect(report.problems).toEqual([]);
      expect(report.relaxed).toHaveLength(1);
      expect(report.relaxed[0]).toContain("Secure");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("configuration from a file on disk", () => {
  /**
   * Next.js reads `.env` before any of this runs, so by the time the console can ask, the file
   * has already contributed. What is left is refusing to serve, which is what a deployment gets.
   */

  it("treats an unset or unknown environment as one that may be configured from a file", () => {
    delete process.env[ENVIRONMENT_VARIABLE];
    expect(fileConfigured()).toBe(true);
    process.env[ENVIRONMENT_VARIABLE] = "development";
    expect(fileConfigured()).toBe(true);
    process.env[ENVIRONMENT_VARIABLE] = "ci";
    expect(fileConfigured()).toBe(true);
    process.env[ENVIRONMENT_VARIABLE] = "test";
    expect(fileConfigured()).toBe(true);
  });

  it("treats a deployment as one that may not", () => {
    process.env[ENVIRONMENT_VARIABLE] = "production";
    expect(fileConfigured()).toBe(false);
    process.env[ENVIRONMENT_VARIABLE] = "staging";
    expect(fileConfigured()).toBe(false);
  });

  it("refuses to start a deployment whose working directory holds one", () => {
    const previous = process.cwd();
    const directory = mkdtempSync(join(tmpdir(), "agentrank-console-"));
    try {
      process.chdir(directory);
      writeFileSync(join(directory, ENV_FILE), "AGENTRANK_CONSOLE_SESSION_SECRET=from-the-file\n");
      process.env[ENVIRONMENT_VARIABLE] = "production";

      const report = inspectConfiguration();

      expect(report.problems).toHaveLength(1);
      expect(report.problems[0]).toContain(ENV_FILE);
      // Named, and never with what was in it.
      expect(report.problems[0]).not.toContain("from-the-file");
      expect(() => {
        requireUsableConfiguration();
      }).toThrow(ENV_FILE);
    } finally {
      process.chdir(previous);
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("leaves a development console with a file alone", () => {
    const previous = process.cwd();
    const directory = mkdtempSync(join(tmpdir(), "agentrank-console-"));
    try {
      process.chdir(directory);
      writeFileSync(join(directory, ENV_FILE), "AGENTRANK_API_BASE_URL=http://localhost:8000\n");
      process.env[ENVIRONMENT_VARIABLE] = "development";

      expect(inspectConfiguration().problems).toEqual([]);
    } finally {
      process.chdir(previous);
      rmSync(directory, { recursive: true, force: true });
    }
  });
});
