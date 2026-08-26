import { LabNav } from "@/components/LabNav";
import { requireConsoleCredential } from "@/lib/auth/credential";

import styles from "./lab.module.css";

/**
 * AgentRank Lab: the operator and investigation surface.
 *
 * Every page under here reads through the same signed in merchant session the product pages
 * use, which is the narrowest trusted mechanism this deployment has: nothing in the Lab is
 * anonymous, and every API it reads is scoped to the merchant the session resolves to, so
 * tenant isolation holds without a second authorization system. What separates the Lab is
 * presentation, not privilege: raw identities, digests, traces and queue state live here so
 * the merchant product does not have to carry them.
 */
export default async function LabLayout({ children }: { children: React.ReactNode }) {
  await requireConsoleCredential();
  return (
    <>
      <div className={styles.bar}>
        <span className={styles.marker}>AgentRank Lab</span>
        <LabNav />
        <span className={styles.note}>Technical instrumentation for your own merchant data.</span>
      </div>
      {children}
    </>
  );
}
