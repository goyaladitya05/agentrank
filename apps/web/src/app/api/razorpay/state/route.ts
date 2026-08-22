/**
 * Read one AgentRank checkout back, so the page can show what the payment actually did.
 *
 * The verification response already carries the authoritative payment attempt. This exists so
 * that the consequence is visible too: a checkout that says PAID is the merchant facing proof
 * that a test payment reached the same business outcome an autonomous one would have.
 */

import { NextResponse } from "next/server";

import { callApi } from "@/lib/agentrank";

export async function GET(request: Request): Promise<NextResponse> {
  const checkoutId = new URL(request.url).searchParams.get("checkout_id");
  if (checkoutId === null || checkoutId.length === 0) {
    return NextResponse.json({ error: "checkout_id is required" }, { status: 400 });
  }

  try {
    const result = await callApi(`/api/v1/commerce/checkouts/${encodeURIComponent(checkoutId)}`, {
      method: "GET",
    });
    return NextResponse.json(result.body, { status: result.status });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "the console could not call the API";
    return NextResponse.json({ error: detail }, { status: 500 });
  }
}
