import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { requestCabDestination, swipeCabCard } from "../src/api.ts";

test("App.tsx contains no unauthenticated car-calls fallback or fake swipe success", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.equal(appSource.includes("/api/v1/operational/car-calls"), false);
  assert.equal(appSource.includes("registered in cab"), false);
});

test("swipeCabCard propagates ACS denial instead of returning fake success", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];

  globalThis.fetch = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return new Response(
      JSON.stringify({
        allowed: false,
        reason_code: "UNREGISTERED_CARD",
        message: "Unregistered credential presented.",
      }),
      { status: 401, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;

  try {
    await assert.rejects(
      () => swipeCabCard("E1", "BAD-CARD", 1),
      /\[UNREGISTERED_CARD\] Unregistered credential presented\./,
    );
    assert.deepEqual(calls, ["/api/v1/acs/auth/cab/swipe"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("requestCabDestination does not fall back to /api/v1/operational/car-calls on denial or 503", async () => {
  const originalFetch = globalThis.fetch;

  for (const [status, reasonCode, message] of [
    [403, "UNAUTHORIZED_FLOOR", "Credential 'RES-10' is not authorized for floor 15 on E1."],
    [503, "DB_FAIL_CLOSED", "Database error: fail-closed policy enforced."],
  ] as const) {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(
        JSON.stringify({ allowed: false, reason_code: reasonCode, message }),
        { status, headers: { "Content-Type": "application/json" } },
      );
    }) as typeof fetch;

    try {
      await assert.rejects(
        () => requestCabDestination("E1", 1, 15),
        new RegExp(`\\[${reasonCode}\\]`),
      );
      assert.deepEqual(calls, ["/api/v1/acs/cab/destination"]);
    } finally {
      globalThis.fetch = originalFetch;
    }
  }
});

test("requestCabDestination returns allowed result on authorized destination", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];

  globalThis.fetch = (async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return new Response(
      JSON.stringify({
        allowed: true,
        message: "Destination floor 10 authorized for E1.",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }) as typeof fetch;

  try {
    const res = await requestCabDestination("E1", 1, 10);
    assert.equal(res.allowed, true);
    assert.equal(res.message, "Destination floor 10 authorized for E1.");
    assert.deepEqual(calls, ["/api/v1/acs/cab/destination"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

