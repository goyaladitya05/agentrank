import type { ReactNode } from "react";

import type { Tone } from "@/lib/labels";

import styles from "./console.module.css";

/**
 * A small filled square plus an uppercase label. The label carries the meaning, so no
 * status ever depends on color alone.
 */
export function StatusMark({
  tone = "neutral",
  label,
  description,
}: {
  tone?: Tone;
  label: string;
  description?: string;
}) {
  return (
    <span className={styles.mark} data-tone={tone} title={description}>
      {label}
    </span>
  );
}

export function Section({
  title,
  hint,
  children,
  actions,
}: {
  title: string;
  hint?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className={styles.section} aria-label={title}>
      <div className={styles.sectionHeader}>
        <h2 className={styles.sectionTitle}>{title}</h2>
        {actions}
        {hint !== undefined ? <span className={styles.sectionHint}>{hint}</span> : null}
      </div>
      {children}
    </section>
  );
}

export function Panel({ children }: { children: ReactNode }) {
  return (
    <div className={styles.panel}>
      <div className={styles.panelBody}>{children}</div>
    </div>
  );
}

export function EmptyState({
  title,
  explanation,
  children,
}: {
  title: string;
  explanation: string;
  /** What a merchant can do about the emptiness, when there is something they can do. */
  children?: ReactNode;
}) {
  return (
    <div className={styles.emptyState}>
      <p className={styles.emptyTitle}>{title}</p>
      <p>{explanation}</p>
      {children === undefined ? null : <p className={styles.finePrintTight}>{children}</p>}
    </div>
  );
}

/**
 * A failure a page can describe. Auth failures link to sign in, missing resources state
 * the fact, everything else offers a retry of the same address.
 */
export function ErrorState({
  title,
  explanation,
  kind,
}: {
  title: string;
  explanation: string;
  kind: "auth" | "missing" | "retry";
}) {
  return (
    <div className={styles.errorBox} role="alert">
      <p className={styles.errorTitle}>{title}</p>
      <p>{explanation}</p>
      {kind === "auth" ? (
        <a className={styles.errorRetry} href="/login">
          Sign in
        </a>
      ) : null}
      {kind === "retry" ? (
        <a className={styles.errorRetry} href="">
          Try again
        </a>
      ) : null}
    </div>
  );
}

export function KeyValueList({
  entries,
}: {
  entries: readonly { term: string; value: ReactNode }[];
}) {
  return (
    <dl className={styles.kv}>
      {entries.map((entry) => (
        <FragmentWithKey key={entry.term} term={entry.term} value={entry.value} />
      ))}
    </dl>
  );
}

function FragmentWithKey({ term, value }: { term: string; value: ReactNode }) {
  return (
    <>
      <dt>{term}</dt>
      <dd>{value}</dd>
    </>
  );
}

export function WarningList({
  warnings,
}: {
  warnings: readonly { code: string; message: string }[];
}) {
  if (warnings.length === 0) {
    return null;
  }
  return (
    <ul className={styles.warningList}>
      {warnings.map((warning) => (
        <li key={`${warning.code}:${warning.message}`} className={styles.warningItem}>
          <span className={styles.warningCode}>{warning.code}</span>
          <span>{warning.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function FinePrint({ children }: { children: ReactNode }) {
  return <p className={styles.finePrint}>{children}</p>;
}
