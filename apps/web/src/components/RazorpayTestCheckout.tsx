"use client";

/**
 * The smallest thing that can open Razorpay Standard Checkout and hand the result back.
 *
 * Integration UI, not product UI. It exists to prove one path end to end: an AgentRank quote
 * becomes a Razorpay order created on the server, a customer pays in Razorpay's own form, and
 * the callback is verified server side before AgentRank believes anything. There is no design
 * system here, no catalog browsing and no cart, because none of that is what Phase 1I is for.
 *
 * Three things this component deliberately does not do.
 *
 * It does not hold a merchant credential. Every call goes to a route handler on the Next.js
 * server, which attaches the key. The browser holds a public Razorpay key id and an order
 * identifier, which is what Standard Checkout needs and what Razorpay's own documentation puts
 * in the page.
 *
 * It does not decide anything about the payment. The handler callback is forwarded verbatim and
 * the server decides whether it is authentic and what it means. A console that could conclude a
 * payment succeeded would be a console that could be told to.
 *
 * It does not send an amount. The amount comes from the admitted payment attempt and is echoed
 * back here for display, so what is rendered can be wrong without what is charged being wrong.
 */

import Script from "next/script";
import { useCallback, useState } from "react";

import {
  formatAmount,
  parseCheckoutState,
  parsePreparation,
  parseVerification,
  type CheckoutHandlerResponse,
  type CheckoutState,
  type PreparedCheckout,
  type VerifiedPayment,
} from "@/lib/razorpay";

import styles from "./RazorpayTestCheckout.module.css";

// Razorpay's own hosted script. Loaded from their origin as their documentation requires, and
// never copied into this repository: a vendored payment script is a payment script that stops
// receiving their fixes.
const CHECKOUT_SCRIPT = "https://checkout.razorpay.com/v1/checkout.js";

interface RazorpayOptions {
  readonly key: string;
  readonly amount: number;
  readonly currency: string;
  readonly name: string;
  readonly description: string;
  readonly order_id: string;
  readonly handler: (response: CheckoutHandlerResponse) => void;
  readonly modal: { readonly ondismiss: () => void };
  readonly theme: { readonly color: string };
}

interface RazorpayInstance {
  open: () => void;
}

declare global {
  interface Window {
    Razorpay?: new (options: RazorpayOptions) => RazorpayInstance;
  }
}

type Phase = "idle" | "preparing" | "awaiting" | "verifying" | "done";

function describe(error: unknown): string {
  return error instanceof Error ? error.message : "something went wrong";
}

async function readJson(response: Response): Promise<unknown> {
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const fields = (body ?? {}) as Record<string, unknown>;
    const detail =
      typeof fields.detail === "string"
        ? fields.detail
        : typeof fields.error === "string"
          ? fields.error
          : `the API answered ${String(response.status)}`;
    throw new Error(detail);
  }
  return body;
}

export function RazorpayTestCheckout({ configured }: { readonly configured: boolean }) {
  const [checkoutId, setCheckoutId] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [prepared, setPrepared] = useState<PreparedCheckout | null>(null);
  const [verified, setVerified] = useState<VerifiedPayment | null>(null);
  const [state, setState] = useState<CheckoutState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async (attemptId: string, response: CheckoutHandlerResponse) => {
    setPhase("verifying");
    try {
      const body = await readJson(
        await fetch("/api/razorpay/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ payment_attempt_id: attemptId, ...response }),
        }),
      );
      const outcome = parseVerification(body);
      setVerified(outcome);
      const current = await readJson(
        await fetch(`/api/razorpay/state?checkout_id=${encodeURIComponent(outcome.checkoutId)}`),
      );
      setState(parseCheckoutState(current));
      setPhase("done");
    } catch (failure) {
      setError(describe(failure));
      setPhase("done");
    }
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setVerified(null);
    setState(null);
    setPhase("preparing");
    try {
      const body = await readJson(
        await fetch("/api/razorpay/prepare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            checkout_id: checkoutId.trim(),
            ...(idempotencyKey.trim().length > 0 ? { idempotency_key: idempotencyKey.trim() } : {}),
          }),
        }),
      );
      const checkout = parsePreparation(body);
      setPrepared(checkout);

      const Razorpay = window.Razorpay;
      if (Razorpay === undefined) {
        throw new Error("the Razorpay checkout script has not loaded yet");
      }
      setPhase("awaiting");
      new Razorpay({
        key: checkout.keyId,
        amount: checkout.amountMinor,
        currency: checkout.currency,
        name: checkout.merchantName,
        description: "AgentRank test mode payment",
        order_id: checkout.orderId,
        handler: (response) => {
          void submit(checkout.paymentAttemptId, response);
        },
        modal: {
          ondismiss: () => {
            // Closing the form is not a failure and not a payment. The order still exists and
            // the same quote can be paid again, which is why nothing is reset here.
            setPhase("idle");
          },
        },
        theme: { color: "#101010" },
      }).open();
    } catch (failure) {
      setError(describe(failure));
      setPhase("idle");
    }
  }, [checkoutId, idempotencyKey, submit]);

  const busy = phase === "preparing" || phase === "verifying" || phase === "awaiting";

  return (
    <section className={styles.panel}>
      <Script src={CHECKOUT_SCRIPT} strategy="afterInteractive" />

      <p className={styles.banner}>
        RAZORPAY TEST MODE. No real money moves. Every payment on this page is a simulated
        transaction against Razorpay test keys.
      </p>

      {!configured && (
        <p className={styles.warning}>
          This console has no merchant API key. Set AGENTRANK_MERCHANT_API_KEY in the Next.js server
          environment and reload.
        </p>
      )}

      <label className={styles.field}>
        <span>AgentRank checkout id</span>
        <input
          value={checkoutId}
          onChange={(event) => {
            setCheckoutId(event.target.value);
          }}
          placeholder="01a02696-916e-703a-8ab5-60c7a93eee4f"
          spellCheck={false}
        />
      </label>

      <label className={styles.field}>
        <span>Idempotency key (optional)</span>
        <input
          value={idempotencyKey}
          onChange={(event) => {
            setIdempotencyKey(event.target.value);
          }}
          placeholder="demo-payment-0001"
          spellCheck={false}
        />
      </label>

      <button
        type="button"
        className={styles.action}
        disabled={busy || !configured || checkoutId.trim().length === 0}
        onClick={() => {
          void start();
        }}
      >
        {busy ? "Working" : "Pay with Razorpay (test mode)"}
      </button>

      {error !== null && <p className={styles.error}>{error}</p>}

      {prepared !== null && (
        <dl className={styles.facts}>
          <dt>Payment attempt</dt>
          <dd>{prepared.paymentAttemptId}</dd>
          <dt>Razorpay order</dt>
          <dd>{prepared.orderId}</dd>
          <dt>Amount</dt>
          <dd>{formatAmount(prepared.amountMinor, prepared.currency)}</dd>
          <dt>Mode</dt>
          <dd>{prepared.testMode ? "test" : "live"}</dd>
        </dl>
      )}

      {verified !== null && (
        <dl className={styles.facts}>
          <dt>Provider payment</dt>
          <dd>{verified.providerState}</dd>
          <dt>Payment attempt</dt>
          <dd>{verified.attemptStatus}</dd>
          <dt>Razorpay checkout</dt>
          <dd>{verified.razorpayStatus}</dd>
          <dt>Settled by this call</dt>
          <dd>{verified.changed ? "yes" : "no, it was already settled"}</dd>
          {state !== null && (
            <>
              <dt>AgentRank checkout</dt>
              <dd>{state.status}</dd>
            </>
          )}
        </dl>
      )}
    </section>
  );
}
