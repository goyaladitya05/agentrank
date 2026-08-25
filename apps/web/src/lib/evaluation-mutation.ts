/**
 * The shape the re-evaluation launch command answers with.
 *
 * Separate from the server action itself because a `"use server"` module may export only async
 * functions, and both the action and the component rendering its result need this.
 */

export interface LaunchState {
  readonly ok: boolean;
  readonly message: string | null;
  /** True when the API refused because state moved, so the page beside this message is fresh. */
  readonly stale: boolean;
  /**
   * True when the console could not tell what happened, which is a different fact from a
   * failure. The launch may or may not have been admitted, so the merchant is told to reload
   * rather than told it failed, and retrying the same form is safe because the request key
   * makes a repeat the same launch.
   */
  readonly unknown: boolean;
  /** The launch this submission produced or found, when the API answered with one. */
  readonly launchId: string | null;
}

export const IDLE_LAUNCH: LaunchState = {
  ok: false,
  message: null,
  stale: false,
  unknown: false,
  launchId: null,
};
