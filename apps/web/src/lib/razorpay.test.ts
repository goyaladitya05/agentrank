import { describe, expect, it } from "vitest";

import {
  formatAmount,
  parseCheckoutState,
  parsePreparation,
  parseVerification,
} from "@/lib/razorpay";

const PREPARED = {
  admitted: true,
  created: true,
  checkout_id: "01a02696-916e-703a-8ab5-60c7a93eee4f",
  refusal: null,
  attempt: null,
  razorpay: {
    payment_attempt_id: "01a02696-9171-74ad-b039-496bd0bbaaaf",
    checkout_id: "01a02696-916e-703a-8ab5-60c7a93eee4f",
    merchant_id: "01a02696-9189-734e-b77b-5dedbd5cde90",
    merchant_name: "Ampere Supply",
    key_id: "rzp_test_0123456789abcd",
    provider_order_id: "order_ABC123",
    provider_receipt: "ar_abcdefghijklmnopqrstuvwxyz234567",
    amount_minor: 499900,
    currency: "INR",
    status: "AWAITING_PAYMENT",
    test_mode: true,
    created: true,
    recovered: false,
    order_created_at: "2026-08-22T07:00:00Z",
  },
};

describe("parsePreparation", () => {
  it("reads what the browser needs to open Standard Checkout", () => {
    const prepared = parsePreparation(PREPARED);

    expect(prepared.keyId).toBe("rzp_test_0123456789abcd");
    expect(prepared.orderId).toBe("order_ABC123");
    expect(prepared.amountMinor).toBe(499900);
    expect(prepared.currency).toBe("INR");
    expect(prepared.testMode).toBe(true);
  });

  it("turns an admission refusal into a message rather than a broken screen", () => {
    // The quote scoped endpoint answers 200 with a machine readable refusal when a payment may
    // not start, because that is a fact about now rather than an error.
    expect(() =>
      parsePreparation({ admitted: false, refusal: "mandate_already_consumed", razorpay: null }),
    ).toThrow(/mandate_already_consumed/);
  });

  it("refuses a response that carries no order", () => {
    expect(() => parsePreparation({ ...PREPARED, razorpay: null })).toThrow();
  });

  it("refuses an amount that is not an integer", () => {
    const wrong = { ...PREPARED, razorpay: { ...PREPARED.razorpay, amount_minor: "499900" } };

    expect(() => parsePreparation(wrong)).toThrow(/amount_minor/);
  });
});

describe("parseVerification", () => {
  it("reads the outcome and the authoritative attempt status", () => {
    const verified = parseVerification({
      confirmed: true,
      changed: true,
      conflicted: false,
      provider_state: "SUCCEEDED",
      checkout_id: "01a02696-916e-703a-8ab5-60c7a93eee4f",
      razorpay_status: "CONFIRMED",
      attempt: { status: "SUCCEEDED" },
    });

    expect(verified.confirmed).toBe(true);
    expect(verified.providerState).toBe("SUCCEEDED");
    expect(verified.attemptStatus).toBe("SUCCEEDED");
    expect(verified.razorpayStatus).toBe("CONFIRMED");
  });

  it("reads an unconfirmed result without pretending it succeeded", () => {
    const verified = parseVerification({
      confirmed: false,
      changed: false,
      conflicted: false,
      provider_state: "PENDING",
      checkout_id: "01a02696-916e-703a-8ab5-60c7a93eee4f",
      razorpay_status: "AWAITING_PAYMENT",
      attempt: { status: "IN_FLIGHT" },
    });

    expect(verified.confirmed).toBe(false);
    expect(verified.providerState).toBe("PENDING");
  });
});

describe("parseCheckoutState", () => {
  it("reads the merchant facing consequence of a payment", () => {
    const state = parseCheckoutState({
      status: "PAID",
      total_amount_minor: 499900,
      currency: "INR",
    });

    expect(state.status).toBe("PAID");
    expect(state.totalAmountMinor).toBe(499900);
  });
});

describe("formatAmount", () => {
  it("renders minor units as an amount a person reads", () => {
    // Display only. Nothing is parsed back out of this and no arithmetic is done on it, which
    // is why a locale difference here cannot change what anybody is charged.
    expect(formatAmount(499900, "INR")).toContain("4,999.00");
  });
});
