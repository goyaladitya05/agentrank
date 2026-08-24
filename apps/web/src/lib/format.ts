/**
 * Presentation formatting for amounts, times and rates.
 *
 * Money is integer minor units with its currency attached, exactly as the backend stores
 * it. Formatting never converts currency and never sums across currencies; those rules
 * live in the components that group values.
 */

export function formatMoney(amountMinor: number, currency: string): string {
  if (!Number.isInteger(amountMinor)) {
    throw new Error(`amount must be an integer count of minor units, got ${String(amountMinor)}`);
  }
  const digits = currencyFractionDigits(currency);
  const major = amountMinor / 10 ** digits;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    currencyDisplay: "code",
  }).format(major);
}

export function formatSignedMoney(deltaMinor: number, currency: string): string {
  const sign = deltaMinor > 0 ? "+" : deltaMinor < 0 ? "\u2212" : "";
  return `${sign}${formatMoney(Math.abs(deltaMinor), currency)}`;
}

function currencyFractionDigits(currency: string): number {
  try {
    const options = new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
    }).resolvedOptions();
    return options.maximumFractionDigits ?? 2;
  } catch {
    return 2;
  }
}

const DATE_FORMAT = new Intl.DateTimeFormat("en-GB", {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});

/** Timestamps render in UTC with the zone stated, so a value always means the same time. */
export function formatTimestamp(iso: string | null): string {
  if (iso === null) {
    return "not recorded";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return `${DATE_FORMAT.format(date)} UTC`;
}

/**
 * Rates are shown as percentages of their suite fixed denominators. A null rate means the
 * denominator was empty, which is reported as such rather than dressed up as zero.
 */
export function formatRate(rate: number | null): string {
  if (rate === null) {
    return "no denominator";
  }
  const percent = rate * 100;
  const rounded = Number.isInteger(percent) ? String(percent) : percent.toFixed(1);
  return `${rounded}%`;
}

export function formatCount(value: number | null, noun: string): string {
  if (value === null) {
    return "Not reported";
  }
  return `${String(value)} ${value === 1 ? noun : `${noun}s`}`;
}

export function truncateMiddle(text: string, max_length: number = 24): string {
  if (text.length <= max_length) {
    return text;
  }
  const half = Math.floor((max_length - 1) / 2);
  return `${text.slice(0, half)}\u2026${text.slice(-half)}`;
}
