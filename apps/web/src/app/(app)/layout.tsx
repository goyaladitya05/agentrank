import Link from "next/link";

import { signOut } from "@/lib/auth/actions";
import { NavLinks } from "@/components/NavLinks";

import styles from "./layout.module.css";

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <header className={styles.header}>
        <div className={styles.brandRow}>
          <Link href="/overview" className={styles.wordmark}>
            AgentRank
          </Link>
          <NavLinks />
          <div className={styles.sessionArea}>
            <Link href="/lab" className={styles.labLink}>
              Lab
            </Link>
            <form action={signOut} className={styles.sessionForm}>
              <button className={styles.signOut} type="submit">
                Sign out
              </button>
            </form>
          </div>
        </div>
      </header>
      <main className={styles.main}>{children}</main>
    </>
  );
}
