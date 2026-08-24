import { SystemStatusTable } from "@/components/SystemStatusTable";
import { apiBaseUrl } from "@/lib/config";
import { fetchSystemStatus } from "@/lib/systemStatus";

import styles from "./page.module.css";

// Status is read on every request. A cached status page reports the past.
export const dynamic = "force-dynamic";

export default async function SystemStatusPage() {
  const target = apiBaseUrl();
  const status = await fetchSystemStatus(target);

  return (
    <>
      <header className={styles.header}>
        <h1 className={styles.wordmark}>AgentRank</h1>
        <span className={styles.target}>{target}</span>
      </header>
      <main className={styles.main}>
        <SystemStatusTable components={[status.api, status.database]} />
      </main>
    </>
  );
}
