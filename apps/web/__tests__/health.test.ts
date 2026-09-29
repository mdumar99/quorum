// @vitest-environment node
import { afterEach, expect, it, vi } from "vitest";

import { getHealth } from "@/lib/health";

const BASE = "http://api.test";

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals(); // never leak a fake fetch into the next test
});

it("returns ok:true with data when the API answers 200", async () => {
  mockFetch(200, { status: "ok", db: "ok", redis: "ok" });
  expect(await getHealth(BASE)).toEqual({
    ok: true,
    httpStatus: 200,
    data: { status: "ok", db: "ok", redis: "ok" },
  });
});

it("treats a 503 as data, not a failure, so the failed dependency is kept", async () => {
  mockFetch(503, { status: "error", db: "error", redis: "ok" });
  expect(await getHealth(BASE)).toEqual({
    ok: true,
    httpStatus: 503,
    data: { status: "error", db: "error", redis: "ok" },
  });
});

it("returns ok:false when the API cannot be reached", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));
  expect(await getHealth(BASE)).toEqual({ ok: false, error: "Could not reach the API" });
});

it("returns ok:false when the API times out", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockRejectedValue(new DOMException("timed out", "TimeoutError")),
  );
  expect(await getHealth(BASE)).toEqual({ ok: false, error: "API did not respond in time" });
});

it("returns ok:false when the body does not match the contract", async () => {
  mockFetch(200, { hello: "world" });
  expect(await getHealth(BASE)).toEqual({ ok: false, error: "Unrecognised response from API" });
});

it("returns ok:false when API_URL is not set", async () => {
  expect(await getHealth("")).toEqual({ ok: false, error: "API_URL is not set" });
});