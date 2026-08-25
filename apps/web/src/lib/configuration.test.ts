import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SESSION_SECRET_VARIABLE } from "@/lib/auth/session";
import { API_BASE_URL_VARIABLE } from "@/lib/config";
import {
  COOKIE_SECURE_VARIABLE,
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
const VARIABLES = [SESSION_SECRET_VARIABLE, API_BASE_URL_VARIABLE, COOKIE_SECURE_VARIABLE];

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
});
