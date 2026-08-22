import { RazorpayTestCheckout } from "@/components/RazorpayTestCheckout";
import { merchantCredential } from "@/lib/agentrank";

import styles from "../page.module.css";

// Whether the console has a credential is read on every request. A cached answer would keep
// telling somebody to set a variable they have already set.
export const dynamic = "force-dynamic";

export default function RazorpayCheckoutPage() {
  // Whether a credential exists, never the credential. This is a server component and the
  // boolean is all that crosses into the browser.
  const configured = merchantCredential() !== null;

  return (
    <>
      <header className={styles.header}>
        <h1 className={styles.wordmark}>AgentRank</h1>
        <span className={styles.target}>razorpay test checkout</span>
      </header>
      <main className={styles.main}>
        <RazorpayTestCheckout configured={configured} />
      </main>
    </>
  );
}
