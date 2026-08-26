"use client";

import Link from "next/link";
import { useActionState } from "react";

import styles from "@/components/console.module.css";
import { EMPTY_VALUES, IDLE_IMPORT, type ImportState } from "@/lib/import-mutation";

/**
 * Naming the public pages AgentRank should read.
 *
 * A form over URLs rather than a crawler with settings. There is no depth, no page budget, no
 * timeout, no header and no selector here, because none of those is a merchant's decision: the
 * API states all of them and a browser cannot raise one. What the merchant supplies is which
 * pages of their own store to read, which is the one thing only they know.
 *
 * The product URLs are a textarea of lines rather than a growing list of inputs. A merchant
 * pastes these from their own admin or their sitemap, and a repeated add-a-row control would be
 * more machinery around the same paste.
 *
 * Split in two so that every state a merchant can land in, including a refusal and a response
 * nobody saw, is renderable in a test without driving a browser.
 */

export type RunImportAction = (
  state: ImportState,
  formData: FormData,
) => ImportState | Promise<ImportState>;

const POLICY_FIELDS = [
  { name: "returns", label: "Returns policy page URL" },
  { name: "warranty", label: "Warranty page URL" },
  { name: "shipping", label: "Shipping page URL" },
] as const;

export function RunImport({ action }: { action: RunImportAction }) {
  const [state, formAction, pending] = useActionState(action, IDLE_IMPORT);
  if (state.ok) {
    return <ImportRead state={state} />;
  }
  return <RunImportForm action={formAction} state={state} pending={pending} />;
}

/**
 * What a merchant is told when the command came back.
 *
 * Deliberately not "your catalog is ready". The pages were read and nothing else happened, and
 * the next thing is the merchant deciding whether what came out is right.
 *
 * An import that ran out of time is also answered 201, with no draft, so "your pages were read"
 * is not a sentence to print unconditionally: it would be telling a merchant something that did
 * not happen. What that import found, which is nothing and the reason, is on its own page.
 */
export function ImportRead({ state }: { state: ImportState }) {
  return (
    <div role="status">
      <p>
        {state.completed
          ? "Your pages were read. Nothing has become your source yet."
          : "This import did not finish. Nothing has become your source."}
      </p>
      {state.importId === null ? null : (
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/sources/imports/${encodeURIComponent(state.importId)}`}
          >
            {state.completed ? "Review what AgentRank read" : "See what happened"}
          </Link>
        </p>
      )}
    </div>
  );
}

export function RunImportForm({
  action,
  state,
  pending,
}: {
  action: string | ((formData: FormData) => void);
  state: ImportState;
  pending: boolean;
}) {
  const values = state.values ?? EMPTY_VALUES;
  // The error element renders only when there is a message and nothing is in flight, so the
  // reference is set on exactly the same condition. Pointing at an element that is not there is a
  // dangling reference for the length of every resubmission.
  const showsError = state.message !== null && !pending;
  return (
    <form action={action} aria-label="Import public pages from your store">
      <p>
        AgentRank reads the pages you name here and turns what they publish into a source draft for
        you to review. Nothing becomes your source until you confirm it.
      </p>
      <ul className={styles.launchTerms}>
        <li>
          Public pages only. AgentRank sends an ordinary page request, signs in to nothing, adds
          nothing to a cart and submits no form on your site.
        </li>
        <li>
          It reads the product data and metadata your pages already publish. It does not read prices
          out of page text, and a page that publishes none is reported rather than guessed at.
        </li>
        <li>Every page must be on the same storefront, and one import may name at most twelve.</li>
      </ul>

      <label className={styles.field}>
        A product page URL
        <input
          name="storefront"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://your-store.example/products/first-product"
          defaultValue={values.storefront}
          aria-describedby={showsError ? "import-error" : undefined}
        />
      </label>

      <label className={styles.field}>
        More product page URLs, one per line
        <textarea
          name="products"
          className={styles.editor}
          rows={6}
          spellCheck={false}
          defaultValue={values.products}
          aria-describedby={showsError ? "import-error" : undefined}
        />
      </label>

      {POLICY_FIELDS.map((field) => (
        <label className={styles.field} key={field.name}>
          {field.label}
          <input
            name={field.name}
            type="url"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            defaultValue={values[field.name]}
            aria-describedby={showsError ? "import-error" : undefined}
          />
        </label>
      ))}

      <div className={styles.buttonRow}>
        <button className={styles.button} type="submit" disabled={pending}>
          Read these pages
        </button>
      </div>
      {pending ? (
        <p className={styles.mutationPending} role="status">
          Reading your pages. This takes a few seconds.
        </p>
      ) : null}
      {showsError ? (
        <p className={styles.mutationAlert} role="alert" id="import-error">
          {state.message}
          {state.stale ? " The state shown here is current." : ""}
        </p>
      ) : null}
    </form>
  );
}
