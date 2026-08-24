/**
 * How often a launch that has not settled is re-read.
 *
 * Restrained on purpose: a benchmark run takes minutes, so a faster interval would only be more
 * requests for the same answer. Here rather than inside the client component so that the page
 * telling a merchant how often it refreshes and the timer that does the refreshing cannot drift
 * apart.
 */
export const REFRESH_SECONDS = 10;
