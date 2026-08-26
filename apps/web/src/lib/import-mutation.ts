/**
 * The shapes the two merchant import commands answer with.
 *
 * Separate from the server actions themselves because a `"use server"` module may export only
 * async functions, and both the actions and the components that render their result need these.
 */

/** What the merchant typed, echoed back so a refusal never empties the form. */
export interface ImportValues {
  readonly storefront: string;
  readonly products: string;
  readonly returns: string;
  readonly warranty: string;
  readonly shipping: string;
}

export const EMPTY_VALUES: ImportValues = {
  storefront: "",
  products: "",
  returns: "",
  warranty: "",
  shipping: "",
};

export interface ImportState {
  readonly ok: boolean;
  readonly message: string | null;
  /** True when the API refused because state moved, so the page beside this message is fresh. */
  readonly stale: boolean;
  /**
   * True when the console could not tell what happened. The import may or may not have run, so
   * the merchant is told to reload rather than told it failed, and submitting the same form again
   * is safe because the request key makes a repeat the same command.
   */
  readonly unknown: boolean;
  /** The import this command produced, when the API answered with one. */
  readonly importId: string | null;
  /**
   * Whether the import finished, because an import that ran out of time is answered 201 with an
   * empty draft. Telling a merchant "your pages were read" for one would be telling them
   * something that did not happen.
   */
  readonly completed: boolean;
  readonly values: ImportValues | null;
}

export const IDLE_IMPORT: ImportState = {
  ok: false,
  message: null,
  stale: false,
  unknown: false,
  importId: null,
  completed: false,
  values: null,
};

export interface ConfirmState {
  readonly ok: boolean;
  readonly message: string | null;
  readonly stale: boolean;
  readonly unknown: boolean;
  /** The snapshot this confirmation resolved to, when the API answered with one. */
  readonly snapshotId: string | null;
  readonly sourceLabel: string | null;
  /**
   * False when the imported document was identical to the merchant's current snapshot, which is
   * what a re-import of an unchanged storefront produces. Nothing was written, and saying "source
   * snapshot created" for that would be telling a merchant their catalog changed when it did not.
   */
  readonly createdSnapshot: boolean;
  /** True when this import had already been confirmed, so this command wrote nothing. */
  readonly alreadyConfirmed: boolean;
  /** What the merchant typed, echoed back so a refusal never discards their own number. */
  readonly values: ConfirmValues | null;
}

/** The one number a merchant states in this workflow. */
export interface ConfirmValues {
  readonly stockLevel: string;
}

export const IDLE_CONFIRM: ConfirmState = {
  ok: false,
  message: null,
  stale: false,
  unknown: false,
  snapshotId: null,
  sourceLabel: null,
  createdSnapshot: false,
  alreadyConfirmed: false,
  values: null,
};
