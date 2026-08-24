import { SystemStatusTable } from "@/components/SystemStatusTable";
import { requireConsoleApiKey } from "@/lib/auth/credential";
import { apiBaseUrl } from "@/lib/config";
import { fetchSystemStatus } from "@/lib/systemStatus";

import styles from "./page.module.css";

// Status is read on every request. A cached status page reports the past.
export const dynamic = "force-dynamic";

export default async function SystemStatusPage() {
  await requireConsoleApiKey();
  const target = apiBaseUrl();
  const status = await fetchSystemStatus(target);

  return (
    <section className={styles.main}>
      <h1 className={styles.wordmark}>System status</h1>
      <SystemStatusTable components={[status.api, status.database]} />
    </section>
  );
}
