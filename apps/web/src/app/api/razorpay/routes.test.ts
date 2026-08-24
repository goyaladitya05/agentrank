/**
 * Security regression coverage for the console's three Razorpay proxy routes.
 *
 * These are the only cookie authenticated write paths in the console that reach commerce, so
 * every property that keeps them safe is asserted here rather than assumed from reading them:
 * an explicit browser session is required, a write needs a same-origin request, one tenant's
 * cookie only ever produces that tenant's credential, a server environment credential never
 * stands in for a signed in merchant, and signing out ends it immediately.
 *
 * The session vault is the real one. Only the cookie jar and the API transport are replaced, so
 * the code under test is the code that ships.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { createSession, destroySession, SESSION_COOKIE } from "@/lib/auth/session";

const cookie: { value: string | undefined } = { value: undefined };

vi.mock("next/headers", () => ({
  cookies: () =>
    Promise.resolve({
      get: (name: string) =>
        name === SESSION_COOKIE && cookie.value !== undefined
          ? { name, value: cookie.value }
          : undefined,
    }),
}));

interface ForwardedCall {
  readonly apiKey: string;
  readonly path: string;
}

const forwarded: ForwardedCall[] = [];

vi.mock("@/lib/agentrank", () => ({
  callApi: (apiKey: string, path: string) => {
    forwarded.push({ apiKey, path });
    return Promise.resolve({ status: 200, body: { forwarded: true } });
  },
}));

const { POST: prepare } = await import("./prepare/route");
const { POST: verify } = await import("./verify/route");
const { GET: state } = await import("./state/route");

const HOST = "console.example";
const CHECKOUT = "01991111-1111-7111-8111-111111111111";
const CALLBACK = {
  payment_attempt_id: "01992222-2222-7222-8222-222222222222",
  razorpay_payment_id: "pay_test",
  razorpay_order_id: "order_test",
  razorpay_signature: "signature_test",
};

function write(
  path: string,
  body: unknown,
  headers: Record<string, string> = { origin: `https://${HOST}`, host: HOST },
): Request {
  return new Request(`https://${HOST}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

function read(query: string): Request {
  return new Request(`https://${HOST}/api/razorpay/state?${query}`, { headers: { host: HOST } });
}

function signIn(apiKey: string): string {
  const token = createSession(apiKey);
  cookie.value = token;
  return token;
}

beforeEach(() => {
  forwarded.length = 0;
  cookie.value = undefined;
  delete process.env.AGENTRANK_MERCHANT_API_KEY;
});

describe("the Razorpay proxy requires an explicit browser session", () => {
  it("refuses a prepare with no session cookie and calls nothing", async () => {
    const response = await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("refuses a verify with no session cookie and calls nothing", async () => {
    const response = await verify(write("/api/razorpay/verify", CALLBACK));
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("refuses a read with no session cookie and calls nothing", async () => {
    const response = await state(read(`checkout_id=${CHECKOUT}`));
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("never accepts a server environment credential in place of a signed in merchant", async () => {
    process.env.AGENTRANK_MERCHANT_API_KEY = "ar_dev_environment_key";
    expect((await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }))).status).toBe(
      401,
    );
    expect((await verify(write("/api/razorpay/verify", CALLBACK))).status).toBe(401);
    expect((await state(read(`checkout_id=${CHECKOUT}`))).status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("stops answering the moment the session is destroyed", async () => {
    const token = signIn("ar_dev_tenant_a");
    expect((await state(read(`checkout_id=${CHECKOUT}`))).status).toBe(200);
    destroySession(token);
    expect((await state(read(`checkout_id=${CHECKOUT}`))).status).toBe(401);
    expect(forwarded).toHaveLength(1);
  });

  it("refuses a cookie that names no session at all", async () => {
    cookie.value = "0".repeat(64);
    expect((await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }))).status).toBe(
      401,
    );
    expect(forwarded).toHaveLength(0);
  });
});

describe("the Razorpay proxy requires a same-origin write", () => {
  beforeEach(() => {
    signIn("ar_dev_tenant_a");
  });

  it("refuses a prepare from another origin even with a valid session", async () => {
    const response = await prepare(
      write(
        "/api/razorpay/prepare",
        { checkout_id: CHECKOUT },
        { origin: "https://attacker.example", host: HOST },
      ),
    );
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("refuses a verify from another origin even with a valid session", async () => {
    const response = await verify(
      write("/api/razorpay/verify", CALLBACK, {
        origin: "https://attacker.example",
        host: HOST,
      }),
    );
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("refuses a write that states no origin at all", async () => {
    const response = await prepare(
      write("/api/razorpay/prepare", { checkout_id: CHECKOUT }, { host: HOST }),
    );
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("refuses a write whose origin header is not a URL", async () => {
    const response = await prepare(
      write("/api/razorpay/prepare", { checkout_id: CHECKOUT }, { origin: "null", host: HOST }),
    );
    expect(response.status).toBe(401);
    expect(forwarded).toHaveLength(0);
  });

  it("accepts a same-origin write and forwards the session's own credential", async () => {
    const response = await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    expect(response.status).toBe(200);
    expect(forwarded).toEqual([
      {
        apiKey: "ar_dev_tenant_a",
        path: `/api/v1/commerce/checkouts/${CHECKOUT}/razorpay-checkout`,
      },
    ]);
  });

  it("validates the callback shape before any credential is used", async () => {
    const response = await verify(
      write("/api/razorpay/verify", { ...CALLBACK, razorpay_signature: "" }),
    );
    expect(response.status).toBe(400);
    expect(forwarded).toHaveLength(0);
  });
});

describe("the Razorpay proxy keeps tenants apart", () => {
  it("forwards only the credential belonging to the cookie that was presented", async () => {
    signIn("ar_dev_tenant_a");
    await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    const stolen = createSession("ar_dev_tenant_b");
    cookie.value = stolen;
    await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    expect(forwarded.map((call) => call.apiKey)).toEqual(["ar_dev_tenant_a", "ar_dev_tenant_b"]);
  });

  it("never lets a request body choose the merchant it acts as", async () => {
    signIn("ar_dev_tenant_a");
    await prepare(
      write("/api/razorpay/prepare", {
        checkout_id: CHECKOUT,
        merchant_id: "01993333-3333-7333-8333-333333333333",
        api_key: "ar_dev_tenant_b",
      }),
    );
    expect(forwarded.map((call) => call.apiKey)).toEqual(["ar_dev_tenant_a"]);
  });
});
