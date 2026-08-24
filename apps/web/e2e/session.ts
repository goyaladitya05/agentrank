import { expect, type BrowserContext, type Page, type TestInfo } from "@playwright/test";

/**
 * Signing a merchant in without recording the credential, and recording everything after it.
 *
 * A Playwright trace records the parameters of every action, the DOM snapshots around it and the
 * network traffic it caused. A merchant API key typed into the sign in form is therefore in the
 * trace three times over: as the argument to `fill`, as the value of the input in the snapshot,
 * and inside the body of the server action the form submits. `retain-on-failure` then writes that
 * to `test-results/` whenever anything downstream breaks, which is a durable credential in a file
 * nobody thinks of as one.
 *
 * The fix is not redaction, because Playwright has none, and not turning traces off, because a
 * browser workflow that fails with no diagnostics is a browser workflow nobody can fix. It is
 * that recording begins after the credential has been submitted. `playwright.config.ts` therefore
 * sets `trace: "off"` and every test starts a trace itself: tests that sign in start it here, at
 * the end of sign in, and tests that never hold a credential start it at the top.
 *
 * `retainOnFailure` reproduces the retention rule the config option of that name has, so the
 * artifact a failure leaves behind is the same one it always was, minus the sign in.
 *
 * What a trace still contains is the console session cookie, because every request the signed in
 * browser makes carries it. That is a random token held in one Next.js server's memory for one
 * test run against a throwaway merchant, and it is not the merchant credential: the credential
 * never enters the browser at all. `scripts/check-e2e-artifacts.py` is what proves that, and it
 * runs over every artifact this harness leaves behind.
 */

/** What a workflow trace captures. The same three the `trace` config option turns on. */
export const RECORDING = { screenshots: true, snapshots: true, sources: true } as const;

/** Begin recording. Safe to call only once per context, which is what every caller does. */
export async function record(context: BrowserContext): Promise<void> {
  await context.tracing.start(RECORDING);
}

/**
 * Write the trace out when the test did not end the way it was expected to, and discard it
 * otherwise. This is `retain-on-failure`, reimplemented because the config option would have
 * started recording before the credential was typed.
 */
export async function retainOnFailure(context: BrowserContext, testInfo: TestInfo): Promise<void> {
  if (testInfo.status !== testInfo.expectedStatus) {
    await context.tracing.stop({ path: testInfo.outputPath("trace.zip") });
    return;
  }
  await context.tracing.stop();
}

/**
 * Sign one merchant in through the real login form, and start recording once they are in.
 *
 * The credential is typed and submitted with nothing watching. Everything a workflow does
 * afterwards is recorded exactly as before.
 */
export async function signIn(page: Page, context: BrowserContext, key: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Merchant API key").fill(key);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("navigation", { name: "Console" })).toBeVisible();
  await record(context);
}
