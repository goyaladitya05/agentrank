/**
 * The shape the evaluation setup command answers with.
 *
 * Separate from the server action itself because a `"use server"` module may export only async
 * functions, and both the action and the component rendering its result need this.
 */

export interface SetupState {
  readonly ok: boolean;
  readonly message: string | null;
  /** True when the API refused because state moved, so the page beside this message is fresh. */
  readonly stale: boolean;
  /**
   * True when the console could not tell what happened, which is a different fact from a
   * failure. Building a setup is identified by the merchant, the snapshot and the generation
   * configuration, so retrying after an unknown answer cannot produce a second setup, and the
   * merchant is told to reload rather than told it failed.
   */
  readonly unknown: boolean;
  /** How many missions the setup this command resolved to holds, when the API answered. */
  readonly missionCount: number | null;
}

export const IDLE_SETUP: SetupState = {
  ok: false,
  message: null,
  stale: false,
  unknown: false,
  missionCount: null,
};
