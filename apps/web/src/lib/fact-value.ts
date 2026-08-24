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
export function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "not stated";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "yes" : "no";
  return JSON.stringify(value);
}
