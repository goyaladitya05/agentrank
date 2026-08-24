/**
 * Sign in for the merchant console.
 *
 * A server rendered form with a server action, so the flow works without client
 * JavaScript and the credential is only ever submitted to this server over one POST.
 * The key is never echoed back, never placed in a URL and never logged.
 */

import { environmentApiKey } from "@/lib/auth/credential";
import { signIn } from "@/lib/auth/actions";

import styles from "./login.module.css";

export const dynamic = "force-dynamic";

const ERROR_MESSAGES: Record<string, string> = {
  empty: "Enter your merchant API key.",
  rejected:
    "The AgentRank API did not accept that key. Check that it belongs to your merchant and has not been revoked.",
  unreachable: "The AgentRank API could not be reached. Confirm it is running and try again.",
  unusable:
    "The AgentRank API answered in a form this console does not understand. Confirm you are pointing at an AgentRank API.",
};

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const params = await searchParams;
  const errorMessage = params.error !== undefined ? (ERROR_MESSAGES[params.error] ?? null) : null;
  const environmentConfigured = environmentApiKey() !== null;

  return (
    <main className={styles.main}>
      <h1 className={styles.wordmark}>AgentRank</h1>
      <p className={styles.lede}>
        Sign in with a merchant API key to view your benchmark insights. Keys are issued by your
        operator with the credentials command line and are verified against the AgentRank API before
        a session is opened.
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
      {environmentConfigured ? (
        <p className={styles.note}>
          A server merchant credential is configured for backend operations. Console access still
          requires an explicit merchant sign-in.
        </p>
      ) : null}
    </main>
  );
}
