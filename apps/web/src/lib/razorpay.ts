/**
 * The shapes the console exchanges with the AgentRank API for a Razorpay test payment, and the
 * parsing that refuses anything else.
 *
 * Parsed rather than cast. `as` would make the compiler agree with a claim nothing checked, and
 * the values here decide what a page renders about a payment. A response of the wrong shape is
 * an error with a sentence in it, not a screen full of `undefined`.
 *
 * Nothing in this module knows a secret. The merchant API key lives on the Next.js server and
 * the Razorpay key secret never leaves the API process. What crosses into the browser is a
 * public Razorpay key id and an order identifier, which is exactly what Standard Checkout needs
 * and exactly what Razorpay's own documentation puts in the page.
 */

export interface PreparedCheckout {
  readonly paymentAttemptId: string;
  readonly checkoutId: string;
  readonly merchantName: string;
  readonly keyId: string;
  readonly orderId: string;
  readonly amountMinor: number;
  readonly currency: string;
  readonly testMode: boolean;
}

export interface VerifiedPayment {
  readonly confirmed: boolean;
  readonly changed: boolean;
  readonly providerState: string;
  readonly attemptStatus: string;
  readonly razorpayStatus: string;
  readonly checkoutId: string;
}

export interface CheckoutState {
  readonly status: string;
  readonly totalAmountMinor: number;
  readonly currency: string;
}

/** The success payload Razorpay's Standard Checkout handler receives. */
export interface CheckoutHandlerResponse {
  readonly razorpay_payment_id: string;
  readonly razorpay_order_id: string;
  readonly razorpay_signature: string;
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null) {
    throw new Error("the API answered with something that is not an object");
  }
  return value as Record<string, unknown>;
}

function text(fields: Record<string, unknown>, key: string): string {
  const value = fields[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`the API answered without a usable ${key}`);
  }
  return value;
}

function integer(fields: Record<string, unknown>, key: string): number {
  const value = fields[key];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new Error(`the API answered without a usable ${key}`);
  }
  return value;
}

function flag(fields: Record<string, unknown>, key: string): boolean {
  const value = fields[key];
  if (typeof value !== "boolean") {
    throw new Error(`the API answered without a usable ${key}`);
  }
  return value;
}

/**
 * The Razorpay half of a preparation response, or a refusal explaining why there is none.
 *
 * The quote scoped endpoint answers 200 with `admitted: false` and a machine readable refusal
 * when a payment may not start, because that is a fact about now rather than an error. This
 * turns the refusal into a message rather than pretending it did not happen.
 */
export function parsePreparation(body: unknown): PreparedCheckout {
  const fields = record(body);
  if (fields.admitted !== true) {
    const refusal = typeof fields.refusal === "string" ? fields.refusal : "unknown";
    throw new Error(`this checkout may not be paid for: ${refusal}`);
  }
  const razorpay = record(fields.razorpay);
  return {
    paymentAttemptId: text(razorpay, "payment_attempt_id"),
    checkoutId: text(razorpay, "checkout_id"),
    merchantName: text(razorpay, "merchant_name"),
    keyId: text(razorpay, "key_id"),
    orderId: text(razorpay, "provider_order_id"),
    amountMinor: integer(razorpay, "amount_minor"),
    currency: text(razorpay, "currency"),
    testMode: flag(razorpay, "test_mode"),
  };
}

export function parseVerification(body: unknown): VerifiedPayment {
  const fields = record(body);
  const attempt = record(fields.attempt);
  return {
    confirmed: flag(fields, "confirmed"),
    changed: flag(fields, "changed"),
    providerState: text(fields, "provider_state"),
    attemptStatus: text(attempt, "status"),
    razorpayStatus: text(fields, "razorpay_status"),
    checkoutId: text(fields, "checkout_id"),
  };
}

export function parseCheckoutState(body: unknown): CheckoutState {
  const fields = record(body);
  return {
    status: text(fields, "status"),
    totalAmountMinor: integer(fields, "total_amount_minor"),
    currency: text(fields, "currency"),
  };
}

/**
 * An amount for a human, from the integer minor units everything else uses.
 *
 * Display only. No arithmetic in this application is done on the result, and nothing is ever
 * parsed back out of it: the API is asked to collect `amountMinor` and the string here is what
 * a person reads while that happens.
 */
export function formatAmount(amountMinor: number, currency: string): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amountMinor / 100);
}
