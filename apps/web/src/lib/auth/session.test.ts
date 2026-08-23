import { beforeEach, describe, expect, it } from "vitest";

import {
  createSession,
  destroySession,
  resolveApiKeyForToken,
  sessionCookieOptions,
  sessionCount,
} from "./session";

const NOW = 1_800_000_000_000;
const TTL_MS = 12 * 60 * 60 * 1000;

const created: string[] = [];
function mint(apiKey: string, nowMs: number): string {
  const token = createSession(apiKey, nowMs);
  created.push(token);
  return token;
}

describe("console sessions", () => {
  beforeEach(() => {
    while (created.length > 0) {
      const token = created.pop();
      if (token !== undefined) {
        destroySession(token);
      }
    }
  });

  it("round trips a session token to its credential", () => {
    const token = mint("ar_dev_a", NOW);
    expect(resolveApiKeyForToken(token, NOW + 1)).toBe("ar_dev_a");
  });

  it("answers null for unknown and malformed tokens without throwing", () => {
    expect(resolveApiKeyForToken(null, NOW)).toBeNull();
    expect(resolveApiKeyForToken(undefined, NOW)).toBeNull();
    expect(resolveApiKeyForToken("", NOW)).toBeNull();
    expect(resolveApiKeyForToken("not-a-token", NOW)).toBeNull();
  });

  it("expires sessions after their lifetime and drops them from the store", () => {
    const token = mint("ar_dev_b", NOW);
    expect(resolveApiKeyForToken(token, NOW + TTL_MS - 1)).toBe("ar_dev_b");
    expect(resolveApiKeyForToken(token, NOW + TTL_MS)).toBeNull();
  });

  it("destroys a session immediately on sign out", () => {
    const token = mint("ar_dev_c", NOW);
    destroySession(token);
    expect(resolveApiKeyForToken(token, NOW + 1)).toBeNull();
  });

  it("destroying an unknown token changes nothing", () => {
    const before = sessionCount();
    destroySession("missing");
    expect(sessionCount()).toBe(before);
  });

  it("caps the vault by evicting sessions when full", () => {
    for (let index = 0; index < 200; index += 1) {
      mint(`ar_dev_${String(index).padStart(3, "0")}`, NOW + index);
    }
    const sizeBefore = sessionCount();
    mint("ar_dev_new", NOW + 10_000);
    expect(sessionCount()).toBeLessThanOrEqual(sizeBefore + 1);
    expect(sessionCount()).toBeLessThanOrEqual(201);
  });

  it("issues distinct tokens for distinct sessions", () => {
    const first = mint("ar_dev_d", NOW);
    const second = mint("ar_dev_e", NOW);
    expect(first).not.toBe(second);
    expect(first).toMatch(/^[0-9a-f]{64}$/);
  });

  it("marks the cookie httpOnly, scoped to the site, and never readable by scripts", () => {
    const options = sessionCookieOptions();
    expect(options.httpOnly).toBe(true);
    expect(options.sameSite).toBe("lax");
    expect(options.path).toBe("/");
    expect(options.maxAge).toBeGreaterThan(0);
  });
});
