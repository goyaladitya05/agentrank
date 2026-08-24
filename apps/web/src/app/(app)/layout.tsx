import { signOut } from "@/lib/auth/actions";
import { NavLinks } from "@/components/NavLinks";

import styles from "./layout.module.css";

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header className={styles.header}>
        <div className={styles.brandRow}>
          <span className={styles.wordmark}>AgentRank</span>
          <NavLinks />
          <form action={signOut} className={styles.sessionForm}>
            <button className={styles.signOut} type="submit">
              Sign out
            </button>
          </form>
        </div>
      </header>
      <main className={styles.main}>{children}</main>
    </>
  );
}
