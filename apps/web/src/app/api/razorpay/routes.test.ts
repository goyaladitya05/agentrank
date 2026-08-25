/**
 * Security regression coverage for the console's three Razorpay proxy routes.
 *
 * These are the only cookie authenticated write paths in the console that reach commerce, so
 * every property that keeps them safe is asserted here rather than assumed from reading them:
 * an explicit browser session is required, a write needs a same-origin request, one browser's
 * cookie only ever produces that browser's credential, a server environment credential never
 * stands in for a signed in merchant, and a cookie naming nothing is refused.
 *
 * The session derivation is the real one. Only the cookie jar and the API transport are
 * replaced, so the code under test is the code that ships. Whether a derived credential still
 * authenticates is now the API's answer rather than this console's, so what these assert is that
 * the right credential is derived and forwarded, and that no request is forwarded without one.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { SESSION_COOKIE, newCookieValue, sessionVerifier } from "@/lib/auth/session";

const SESSION_SECRET = "a-console-session-secret-of-sufficient-length";
process.env.AGENTRANK_CONSOLE_SESSION_SECRET = SESSION_SECRET;

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
  readonly credential: string;
  readonly path: string;
}

const forwarded: ForwardedCall[] = [];

vi.mock("@/lib/agentrank", () => ({
  callApi: (credential: string, path: string) => {
    forwarded.push({ credential, path });
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

/** One signed in browser: a fresh cookie, and the credential this console derives from it. */
function signIn(): string {
  cookie.value = newCookieValue();
  const derived = sessionVerifier(cookie.value);
  if (derived === null) throw new Error("the console must derive a credential from its cookie");
  return derived;
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

  it("stops answering the moment the cookie is cleared, which is what signing out does", async () => {
    signIn();
    expect((await state(read(`checkout_id=${CHECKOUT}`))).status).toBe(200);
    cookie.value = undefined;
    expect((await state(read(`checkout_id=${CHECKOUT}`))).status).toBe(401);
    expect(forwarded).toHaveLength(1);
  });

  it("refuses a cookie of the wrong shape without deriving anything from it", async () => {
    cookie.value = "0".repeat(64);
    expect((await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }))).status).toBe(
      401,
    );
    expect(forwarded).toHaveLength(0);
  });
});

describe("the Razorpay proxy requires a same-origin write", () => {
  beforeEach(() => {
    signIn();
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
    const expected = sessionVerifier(cookie.value);
    const response = await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    expect(response.status).toBe(200);
    expect(forwarded).toEqual([
      {
        credential: expected,
        path: `/api/v1/commerce/checkouts/${CHECKOUT}/razorpay-checkout`,
      },
    ]);
    // What travels to the API is the derived credential, never the browser's cookie.
    expect(forwarded[0]?.credential).not.toBe(cookie.value);
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
    const first = signIn();
    await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    const second = signIn();
    await prepare(write("/api/razorpay/prepare", { checkout_id: CHECKOUT }));
    expect(first).not.toBe(second);
    expect(forwarded.map((call) => call.credential)).toEqual([first, second]);
  });

  it("never lets a request body choose the merchant it acts as", async () => {
    const own = signIn();
    await prepare(
      write("/api/razorpay/prepare", {
        checkout_id: CHECKOUT,
        merchant_id: "01993333-3333-7333-8333-333333333333",
        api_key: `ar_dev_${"a".repeat(32)}_${"b".repeat(64)}`,
      }),
    );
    expect(forwarded.map((call) => call.credential)).toEqual([own]);
  });
});
