"use client";

import Link from "next/link";
import { useActionState } from "react";

import { KeyValueList, StatusMark } from "@/components/Primitives";
import styles from "@/components/console.module.css";
import { formatTimestamp } from "@/lib/format";
import {
  availabilityLabel,
  extractionLabel,
  formatImportedPrice,
  type ImportNote,
  type ImportPage,
  type ImportPolicy,
  type ImportProduct,
  type SourceImport,
} from "@/lib/import";
import { IDLE_CONFIRM, type ConfirmState } from "@/lib/import-mutation";

/**
 * What AgentRank read from a merchant's own pages, before any of it is source history.
 *
 * Evidence first, and deliberately unexciting. Every number on this page is a count of something
 * that happened, every product names the page and the method behind it, and everything AgentRank
 * could not read is listed with the reason rather than quietly absent. There is no score, no
 * grade, no suggestion and no claim that anything was understood.
 *
 * Two things are rendered with particular care.
 *
 * **Availability is never a quantity.** A page saying "In stock" published no number, so the table
 * says exactly that. The number the evaluation world will hold is asked for once, below, and is
 * labelled as the merchant's statement rather than as something imported.
 *
 * **Every string here came from somebody else's web page.** All of it is rendered as text. No
 * value becomes markup, and a URL is shown as text rather than turned into a link: a link to a
 * page AgentRank fetched is a navigation this console has no reason to offer, and a place where a
 * value would become behaviour.
 */

export type ConfirmImportAction = (
  state: ConfirmState,
  formData: FormData,
) => ConfirmState | Promise<ConfirmState>;

export function ImportReview({
  found,
  action,
}: {
  found: SourceImport;
  action: ConfirmImportAction;
}) {
  return (
    <>
      <Summary found={found} />
      <Pages pages={found.pages} />
      <Products products={found.products} />
      <Policies policies={found.policies} />
      <Notes
        title="Not imported"
        empty="Everything these pages published was imported."
        notes={found.omissions}
      />
      <Notes
        title="Worth knowing"
        empty="Nothing else to report about what was imported."
        notes={found.findings}
      />
      <Confirm found={found} action={action} />
    </>
  );
}

function Summary({ found }: { found: SourceImport }) {
  const summary = found.summary;
  return (
    <KeyValueList
      entries={[
        { term: "Storefront", value: summary.origin },
        { term: "Read", value: formatTimestamp(summary.created_at) },
        {
          term: "Pages",
          value: `${String(summary.retrieved_count)} of ${String(summary.page_count)} answered`,
        },
        {
          term: "Extracted",
          value: `${String(summary.product_count)} product(s), ${String(summary.variant_count)} variant(s), ${String(summary.policy_count)} policy text(s)`,
        },
        {
          term: "Not imported",
          value:
            summary.omission_count > 0
              ? `${String(summary.omission_count)} item(s), listed below`
              : summary.state === "COMPLETED"
                ? "Nothing"
                : "Not known, because this import did not finish",
        },
        {
          term: "Finished",
          value:
            summary.state === "COMPLETED"
              ? "Yes"
              : `No. ${summary.failure_reason === "deadline" ? "This import ran out of time before it could read every page." : "This import did not finish."}`,
        },
        {
          term: "Source snapshot",
          value:
            summary.source_snapshot_id === null
              ? "Not created. Nothing here is your source yet."
              : "Created from this import",
        },
      ]}
    />
  );
}

function Pages({ pages }: { pages: readonly ImportPage[] }) {
  return (
    <>
      <h3 className={styles.sectionTitle}>Pages read</h3>
      <div className={styles.tableScroll} tabIndex={0} aria-label="Pages read">
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Page</th>
              <th scope="col">Kind</th>
              <th scope="col">Answered</th>
              <th scope="col">Size</th>
              <th scope="col">Content digest</th>
            </tr>
          </thead>
          <tbody>
            {pages.map((entry) => (
              <tr key={entry.url}>
                <td>
                  <span className={styles.mono}>{entry.url}</span>
                  {entry.redirect_count > 0 ? (
                    <>
                      <br />
                      <span className={styles.cellMuted}>
                        Redirected {String(entry.redirect_count)} time(s) to {entry.final_url}
                      </span>
                    </>
                  ) : null}
                </td>
                <td>{entry.kind === "POLICY" ? (entry.name ?? "Policy") : "Product"}</td>
                <td>
                  {entry.retrieved ? (
                    <StatusMark
                      tone="ok"
                      label={
                        entry.status_code === null ? "Read" : `HTTP ${String(entry.status_code)}`
                      }
                    />
                  ) : (
                    <>
                      <StatusMark tone="fail" label="Not read" />
                      <br />
                      <span className={styles.cellMuted}>{entry.detail}</span>
                    </>
                  )}
                </td>
                <td>{entry.retrieved ? `${String(entry.byte_count)} bytes` : "0 bytes"}</td>
                <td>
                  <span className={styles.mono}>{shortened(entry.content_hash)}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Products({ products }: { products: readonly ImportProduct[] }) {
  if (products.length === 0) {
    return (
      <>
        <h3 className={styles.sectionTitle}>Products extracted</h3>
        <p>No product could be extracted from these pages.</p>
      </>
    );
  }
  return (
    <>
      <h3 className={styles.sectionTitle}>Products extracted</h3>
      <div className={styles.tableScroll} tabIndex={0} aria-label="Products extracted">
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Product</th>
              <th scope="col">Variant</th>
              <th scope="col">Price</th>
              <th scope="col">Availability</th>
              <th scope="col">Read from</th>
            </tr>
          </thead>
          <tbody>
            {products.flatMap((product) =>
              product.variants.map((variant, index) => (
                <tr key={`${product.external_id}:${variant.sku}`}>
                  <td>
                    {index === 0 ? (
                      <>
                        {product.title}
                        <br />
                        <span className={styles.cellMuted}>
                          {product.category ?? "No category published"}
                        </span>
                        <br />
                        <span className={styles.cellMuted}>
                          {product.description ?? "No description published"}
                        </span>
                      </>
                    ) : null}
                  </td>
                  <td>
                    <span className={styles.mono}>{variant.sku}</span>
                    {variant.label === null ? null : (
                      <>
                        <br />
                        <span className={styles.cellMuted}>{variant.label}</span>
                      </>
                    )}
                  </td>
                  <td>{formatImportedPrice(variant.price_amount_minor, variant.currency)}</td>
                  <td>
                    {availabilityLabel(variant.availability, variant.inventory_quantity)}
                    {variant.availability_text === null ? null : (
                      <>
                        <br />
                        <span className={styles.cellMuted}>
                          Page says: {variant.availability_text}
                        </span>
                      </>
                    )}
                  </td>
                  <td>
                    {index === 0 ? (
                      <>
                        {extractionLabel(product.extraction)}
                        <br />
                        <span className={styles.mono}>{product.source_url}</span>
                      </>
                    ) : null}
                  </td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Policies({ policies }: { policies: readonly ImportPolicy[] }) {
  if (policies.length === 0) return null;
  return (
    <>
      <h3 className={styles.sectionTitle}>Policy text</h3>
      {policies.map((policy) => (
        <div key={policy.name}>
          <p className={styles.reviewMeta}>
            {policy.name} from {policy.source_url}
            {policy.truncated ? ", cut to the length a source document holds" : ""}
          </p>
          <p className={styles.policyBody}>{policy.body}</p>
        </div>
      ))}
    </>
  );
}

function Notes({
  title,
  empty,
  notes,
}: {
  title: string;
  empty: string;
  notes: readonly ImportNote[];
}) {
  return (
    <>
      <h3 className={styles.sectionTitle}>{title}</h3>
      {notes.length === 0 ? (
        <p className={styles.reviewMeta}>{empty}</p>
      ) : (
        <ul className={styles.warningList}>
          {notes.map((note) => (
            <li
              key={`${note.code}:${note.source_url}:${note.subject ?? ""}`}
              className={styles.warningItem}
            >
              <span className={styles.warningCode}>{note.code}</span>
              <span>
                {note.detail}
                <br />
                <span className={styles.mono}>{note.source_url}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * The one command on this page, and it asks for nothing.
 *
 * A confirmation is a decision about evidence the merchant has already read, and every fact in
 * the snapshot it creates came off their own pages. It used to ask for a stock level, because a
 * source variant needed an exact count and no public page publishes one; a source variant now
 * holds the availability state a storefront actually publishes, so the last field on this page
 * that was not evidence is gone.
 *
 * What is still said out loud is the variants whose pages published no availability at all. That
 * is stored honestly and an evaluation world cannot hold it, so the merchant is told which lines
 * they will have to state a stock state for, and where.
 */
function Confirm({ found, action }: { found: SourceImport; action: ConfirmImportAction }) {
  const [state, formAction, pending] = useActionState(action, IDLE_CONFIRM);
  if (state.ok) {
    return <Confirmed state={state} />;
  }
  if (found.summary.source_snapshot_id !== null) {
    return (
      <>
        <h3 className={styles.sectionTitle}>Source snapshot</h3>
        <p role="status">
          This import has already been confirmed and is part of your source history.
        </p>
        <p className={styles.reviewMeta}>
          <Link
            className={styles.rowLink}
            href={`/sources/${encodeURIComponent(found.summary.source_snapshot_id)}`}
          >
            Open the source snapshot it created
          </Link>
        </p>
      </>
    );
  }
  const showsError = state.message !== null && !pending;
  return (
    <>
      <h3 className={styles.sectionTitle}>Create the source snapshot</h3>
      {found.blockers.length > 0 ? (
        <>
          <p>This import cannot become a source snapshot yet.</p>
          <ul className={styles.warningList} id="confirm-blockers">
            {found.blockers.map((blocker) => (
              <li key={blocker.code} className={styles.warningItem}>
                <span className={styles.warningCode}>{blocker.code}</span>
                <span>{blocker.detail}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      <form action={formAction} aria-label="Create a source snapshot from this import">
        <ul className={styles.launchTerms}>
          <li>
            Confirming stores an immutable source snapshot of exactly what is shown above. Your
            existing snapshots never change.
          </li>
          <li>Nothing is compiled and no evaluation runs. Both remain separate commands.</li>
          <li>
            This does not change any price, stock level or order on your store. AgentRank only read
            your pages.
          </li>
        </ul>
        {found.unstated_availability.length > 0 ? (
          <p className={styles.reviewMeta} id="unstated-availability">
            {found.unstated_availability.length === 1
              ? "One variant's page did not say whether it can be bought"
              : `${String(found.unstated_availability.length)} variants' pages did not say whether they can be bought`}
            {": "}
            {found.unstated_availability.join(", ")}. That is stored exactly as it stands. An
            evaluation world holds an exact number of units and cannot hold an unknown, so you will
            be asked to state whether these are in stock before an evaluation setup can be built
            from this snapshot.
          </p>
        ) : null}
        <div className={styles.buttonRow}>
          <button className={styles.button} type="submit" disabled={pending || !found.confirmable}>
            Create source snapshot
          </button>
        </div>
        {pending ? (
          <p className={styles.mutationPending} role="status">
            Creating your source snapshot
          </p>
        ) : null}
        {showsError ? (
          <p className={styles.mutationAlert} role="alert" id="confirm-error">
            {state.message}
            {state.stale && !state.unknown ? " The state shown here is current." : ""}
          </p>
        ) : null}
      </form>
    </>
  );
}

export function Confirmed({ state }: { state: ConfirmState }) {
  return (
    <>
      <h3 className={styles.sectionTitle}>Source snapshot</h3>
      <div role="status">
        <p>{outcome(state)}</p>
        {state.snapshotId === null ? null : (
          <>
            <p className={styles.reviewMeta}>
              <Link
                className={styles.rowLink}
                href={`/sources/${encodeURIComponent(state.snapshotId)}`}
              >
                Open this source snapshot
              </Link>
            </p>
            <p className={styles.reviewMeta}>
              <Link className={styles.rowLink} href="/evaluations">
                Continue to your evaluation setup
              </Link>
            </p>
          </>
        )}
      </div>
    </>
  );
}

/**
 * What a confirmation actually did, in one sentence, distinguishing three different facts.
 *
 * A new snapshot. A document identical to the merchant's current snapshot, which is what a
 * re-import of an unchanged storefront produces and which wrote nothing. And an import that had
 * already been confirmed, where this command wrote nothing either and the snapshot it names was
 * created by an earlier one.
 */
function outcome(state: ConfirmState): string {
  if (state.alreadyConfirmed) {
    return "This import had already been confirmed, so nothing was written. It names the source snapshot an earlier confirmation created.";
  }
  if (state.createdSnapshot) {
    return `Source snapshot ${state.sourceLabel ?? ""} created. Nothing has been compiled and no evaluation has run.`;
  }
  return "This import says the same thing as your current source snapshot, so no new snapshot was created.";
}

/** A content digest, short enough for a table cell and long enough to compare two by eye. */
function shortened(hash: string | null): string {
  if (hash === null) return "";
  const value = hash.replace(/^sha256:/, "");
  return `${value.slice(0, 12)}...`;
}
