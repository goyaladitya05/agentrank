import { RazorpayTestCheckout } from "@/components/RazorpayTestCheckout";
import { requireConsoleCredential } from "@/lib/auth/credential";

import styles from "./page.module.css";

// Whether the console has a credential is read on every request. A cached answer would keep
// telling somebody to set a variable they have already set.
export const dynamic = "force-dynamic";

export default async function RazorpayCheckoutPage() {
  await requireConsoleCredential();

  return (
    <>
      <header className={styles.header}>
        <h1 className={styles.wordmark}>AgentRank</h1>
        <span className={styles.target}>razorpay test checkout</span>
      </header>
      <main className={styles.main}>
        <RazorpayTestCheckout configured />
      </main>
    </>
  );
}
