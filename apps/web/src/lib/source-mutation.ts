/**
 * The shapes the two source workflow commands answer with.
 *
 * Separate from the server actions themselves because a `"use server"` module may export only
 * async functions, and both the actions and the components that render their result need these.
 */

/** What the merchant typed, echoed back so a refusal never empties the editor. */
export interface SourceValues {
  readonly document: string;
}

export interface SourceSubmissionState {
  readonly ok: boolean;
  readonly message: string | null;
  /** True when the API refused because state moved, so the page beside this message is fresh. */
  readonly stale: boolean;
  /**
   * True when the console could not tell what happened. The submission may or may not have been
   * accepted, so the merchant is told to reload rather than told it failed, and submitting the
   * same form again is safe because the request key makes a repeat the same command.
   */
  readonly unknown: boolean;
  /** The snapshot this command resolved to, when the API answered with one. */
  readonly snapshotId: string | null;
  /** False when the evidence matched the current snapshot and nothing new was written. */
  readonly createdSnapshot: boolean;
  readonly values: SourceValues | null;
}

export const IDLE_SUBMISSION: SourceSubmissionState = {
  ok: false,
  message: null,
  stale: false,
  unknown: false,
  snapshotId: null,
  createdSnapshot: false,
  values: null,
};

export interface CompileState {
  readonly ok: boolean;
  readonly message: string | null;
  readonly stale: boolean;
  readonly unknown: boolean;
  /** The run this command created or found, when the API answered with one. */
  readonly runId: string | null;
  /**
   * What the run is, so the acknowledgement can say something true.
   *
   * A compiler run that could not read its snapshot completes FAILED and is still answered 201; a
   * well formed document often produces nothing to review at all; asking again for a snapshot
   * already compiled and published answers with that run; and a row left PENDING or RUNNING by an
   * older build, which nothing can create now, is none of those. "The facts it proposed are
   * waiting for your review" is false in all four, and the API already told the console which one
   * this is.
   */
  readonly runStatus: string | null;
  readonly pendingReviews: number | null;
  readonly published: boolean;
}

export const IDLE_COMPILE: CompileState = {
  ok: false,
  message: null,
  stale: false,
  unknown: false,
  runId: null,
  runStatus: null,
  pendingReviews: null,
  published: false,
};
