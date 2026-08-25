/**
 * Sign in for the merchant console.
 *
 * A server rendered form with a server action, so the flow works without client
 * JavaScript and the credential is only ever submitted to this server over one POST.
 * The key is never echoed back, never placed in a URL and never logged.
 */

import { signIn } from "@/lib/auth/actions";

import styles from "./login.module.css";

export const dynamic = "force-dynamic";

const ERROR_MESSAGES: Record<string, string> = {
  empty: "Enter your merchant API key.",
  rejected:
    "The AgentRank API did not accept that key. Check that it belongs to your merchant and has not been revoked.",
  unreachable: "The AgentRank API could not be reached. Confirm it is running and try again.",
  unusable:
    "The AgentRank API answered in a form this console does not understand, or this console is not configured to hold sessions. Confirm you are pointing at an AgentRank API and that the deployment has a session secret.",
  expired: "Your session has ended. Sign in again to continue.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const params = await searchParams;
  const errorMessage = params.error !== undefined ? (ERROR_MESSAGES[params.error] ?? null) : null;

  return (
    <main className={styles.main}>
      <h1 className={styles.wordmark}>AgentRank</h1>
      <p className={styles.lede}>
        Sign in with a merchant API key to view your benchmark insights. Keys are issued by your
        operator with the credentials command line. The key opens a session on the AgentRank API and
        is not kept by this console afterwards; your browser holds only an opaque session cookie.
      </p>
      {errorMessage !== null ? (
        <p className={styles.error} role="alert">
          {errorMessage}
        </p>
      ) : null}
      <form action={signIn} className={styles.form}>
        <label className={styles.label} htmlFor="apiKey">
          Merchant API key
        </label>
        <input
          className={styles.input}
          id="apiKey"
          name="apiKey"
          type="password"
          autoComplete="off"
          spellCheck={false}
          required
        />
        <button className={styles.submit} type="submit">
          Sign in
        </button>
      </form>
    </main>
  );
}
