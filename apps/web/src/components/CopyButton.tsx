"use client";

import { useState } from "react";

import styles from "./console.module.css";

/**
 * Copies a technical identifier. The only interactive affordance on data pages, and it
 * holds nothing but the text it was given.
 */
export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }

  return (
    <span className={styles.copyDone}>
      <button
        type="button"
        className={styles.copyButton}
        onClick={() => {
          void copy();
        }}
        aria-label={`${label}: ${value}`}
      >
        {label}
      </button>
      <span role="status" aria-live="polite">
        {copied ? " Copied" : ""}
      </span>
    </span>
  );
}
