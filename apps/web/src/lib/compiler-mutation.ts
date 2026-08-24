/**
 * The shape a compiler write command answers with.
 *
 * Separate from the server actions themselves because a `"use server"` module may export only
 * async functions, and both the actions and the components that render their result need this.
 */

/** What the merchant typed, echoed back so a refusal never empties the form. */
export interface CorrectionValues {
  readonly value: string;
  readonly provenanceField: string;
  readonly provenanceExcerpt: string;
}

export interface CompilerMutationState {
  readonly ok: boolean;
  readonly message: string | null;
  /** True when the API refused because state moved, so the page beside this message is fresh. */
  readonly stale: boolean;
  readonly values: CorrectionValues | null;
}

export const IDLE_MUTATION: CompilerMutationState = {
  ok: false,
  message: null,
  stale: false,
  values: null,
};
