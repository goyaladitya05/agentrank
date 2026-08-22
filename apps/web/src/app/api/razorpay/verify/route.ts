/**
 * Hand a Standard Checkout success payload to the AgentRank API for verification.
 *
 * The browser posts what Razorpay gave it and this forwards it with the merchant credential
 * attached. Nothing is verified here: the signature check needs the Razorpay key secret, which
 * is in the API process and nowhere else, and a verification the console could perform would be
 * a verification the console could skip.
 */

import { NextResponse } from "next/server";

import { callApi } from "@/lib/agentrank";

const REQUIRED = ["razorpay_payment_id", "razorpay_order_id", "razorpay_signature"] as const;

export async function POST(request: Request): Promise<NextResponse> {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ error: "the request body was not JSON" }, { status: 400 });
  }

  const fields = payload as Record<string, unknown>;
  const attemptId = fields.payment_attempt_id;
  if (typeof attemptId !== "string" || attemptId.length === 0) {
    return NextResponse.json({ error: "payment_attempt_id is required" }, { status: 400 });
  }

  const callback: Record<string, string> = {};
  for (const field of REQUIRED) {
    const value = fields[field];
    if (typeof value !== "string" || value.length === 0) {
      return NextResponse.json({ error: `${field} is required` }, { status: 400 });
    }
    callback[field] = value;
  }

  try {
    const result = await callApi(
      `/api/v1/commerce/payments/${encodeURIComponent(attemptId)}/razorpay-checkout/verify`,
      { method: "POST", body: callback },
    );
    return NextResponse.json(result.body, { status: result.status });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "the console could not call the API";
    return NextResponse.json({ error: detail }, { status: 500 });
  }
}
