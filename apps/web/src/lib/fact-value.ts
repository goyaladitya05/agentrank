/**
 * Reading one Commerce IR fact value as prose.
 *
 * A fact value is JSON: a wattage is a number, an availability is a four-state string, and a
 * price is an object. Only the scalars are worth a sentence, so an object falls back to its JSON
 * rather than pretending to be one.
 *
 * Its own module rather than a component export, because both a server rendered table cell and a
 * client review form need it, and a client module cannot hand a plain function to the server.
 */
import { formatMoney } from "@/lib/format";

export function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "not stated";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "yes" : "no";
  const money = asMoney(value);
  if (money !== null) return money;
  return JSON.stringify(value);
}

/**
 * A price fact rendered as the amount it is, not as its JSON. Integer minor units with the
 * currency attached, matching how the backend stores money; anything that is not exactly
 * that shape falls through to JSON rather than being guessed at.
 */
function asMoney(value: unknown): string | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as { currency?: unknown; amount_minor?: unknown };
  if (typeof record.currency !== "string" || typeof record.amount_minor !== "number") return null;
  if (!Number.isInteger(record.amount_minor) || Object.keys(record).length !== 2) return null;
  try {
    return formatMoney(record.amount_minor, record.currency);
  } catch {
    return null;
  }
}
