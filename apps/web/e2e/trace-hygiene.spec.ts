import { execFileSync } from "node:child_process";

import { expect, test } from "@playwright/test";

import configuration from "../playwright.config";
import { RECORDING, signIn } from "./session";

/**
 * What a browser test run actually leaves on disk, checked rather than assumed.
 *
 * The workflow specs sign a real merchant in, and a Playwright trace records the arguments of
 * every action, the DOM around it and the network it caused. The harness keeps the credential
 * out of the trace by starting recording after sign in. Two things could quietly undo that: a
 * configured `trace` option, which would start recording before the key is typed, and a future
 * change to how sign in works. Both are checked here.
 *
 * The trace this produces is retained on purpose, in the same directory a failure would write
 * one to. That is what gives the repository-wide scan in `make test-browser` something real to
 * read: a scan that passes because it found no artifacts is a scan that keeps passing after
 * somebody stops producing them.
 */

const key = process.env.AGENTRANK_E2E_KEY;

test("the harness records nothing around sign in", () => {
  // A configured trace begins before the first action of the test, which is the one that types
  // the credential. Only a trace each test starts itself can begin after it.
  expect(configuration.use?.trace).toBe("off");
});

test("a retained trace of a signed in session carries no credential material", async ({
  browser,
  baseURL,
}, testInfo) => {
  if (key === undefined) throw new Error("AGENTRANK_E2E_KEY is required");
  // Its own context, so this test owns the whole trace rather than sharing the one the workflow
  // specs retain only on failure.
  const context = await browser.newContext(baseURL === undefined ? {} : { baseURL });
  const page = await context.newPage();

  await signIn(page, context, key);
  await page
    .getByRole("navigation", { name: "Console" })
    .getByRole("link", { name: "Compiler" })
    .click();
  await expect(page.getByRole("heading", { name: "Compiler review" })).toBeVisible();

  const trace = testInfo.outputPath("signed-in-trace.zip");
  await context.tracing.stop({ path: trace });
  await context.close();

  // The scanner opens every entry in the zip, so a credential a compressed trace was hiding is
  // found where a substring search over the file would report success. `--require` makes the
  // clean answer mean something: it fails unless the trace really did capture the session.
  const report = execFileSync(
    "uv",
    ["run", "python", "scripts/check-e2e-artifacts.py", trace, "--require", "/compiler"],
    {
      cwd: "../..",
      encoding: "utf-8",
      // Through the environment rather than the argument vector, which every process on the
      // machine can read.
      env: { ...process.env, AGENTRANK_E2E_SECRETS: key },
    },
  );

  expect(report).toContain("no credential material");
  // The trace was worth scanning: recording covered the signed in navigation and its snapshots.
  expect(RECORDING.snapshots).toBe(true);
});
