/**
 * Prepare a Razorpay checkout for an AgentRank quote, on the server, with the merchant key.
 *
 * A thin proxy and deliberately nothing more. It attaches the credential the browser must never
 * hold, forwards one identifier, and passes the API's answer back with its status intact. It
 * makes no decision, so there is no decision here that could disagree with the one the API made.
 */

import { NextResponse } from "next/server";

import { callApi } from "@/lib/agentrank";
import { mutationCredential } from "@/lib/auth/request";

export async function POST(request: Request): Promise<NextResponse> {
  const credential = await mutationCredential(request);
  if (credential === null) {
    return NextResponse.json(
      { error: "authenticated same-origin session required" },
      { status: 401 },
    );
  }
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json({ error: "the request body was not JSON" }, { status: 400 });
  }

  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return NextResponse.json({ error: "request body must be an object" }, { status: 400 });
  }
  const fields = payload as Record<string, unknown>;
  const checkoutId = fields.checkout_id;
  const idempotencyKey = fields.idempotency_key;
  if (typeof checkoutId !== "string" || checkoutId.length === 0) {
    return NextResponse.json({ error: "checkout_id is required" }, { status: 400 });
  }

  try {
    const result = await callApi(
      credential,
      `/api/v1/commerce/checkouts/${encodeURIComponent(checkoutId)}/razorpay-checkout`,
      {
        method: "POST",
        body: typeof idempotencyKey === "string" ? { idempotency_key: idempotencyKey } : {},
      },
    );
    return NextResponse.json(result.body, { status: result.status });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "the console could not call the API";
    return NextResponse.json({ error: detail }, { status: 500 });
  }
}
