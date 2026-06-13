import assert from "node:assert/strict";
import test from "node:test";
import { retryableError, sleep } from "../src/retry.js";

test("retryableError matches genuine transient failures", () => {
  assert.equal(retryableError(new Error("Anthropic API error (503): service unavailable")), true);
  assert.equal(retryableError(new Error("request failed with status 500")), true);
  assert.equal(retryableError(new Error("429 Too Many Requests")), true);
  assert.equal(retryableError(new Error("socket network timeout")), true);
  assert.equal(retryableError(new Error("overloaded_error")), true);
});

test("retryableError ignores numbers that merely contain a 5xx substring", () => {
  // Regression: msg.includes("500") matched "5000", forcing ~21s of pointless
  // retries on a non-retryable 400.
  assert.equal(
    retryableError(new Error("Anthropic API error (400): max_tokens: 5000 > model maximum")),
    false,
  );
  assert.equal(retryableError(new Error("not_found_error: invalid model id")), false);
});

test("sleep resolves immediately when the signal is already aborted", async () => {
  const controller = new AbortController();
  controller.abort();
  const start = Date.now();
  await sleep(10_000, controller.signal);
  assert.ok(Date.now() - start < 1_000, "aborted sleep must not wait out the full delay");
});

test("sleep wakes early when the signal aborts mid-wait", async () => {
  const controller = new AbortController();
  const start = Date.now();
  const pending = sleep(10_000, controller.signal);
  controller.abort();
  await pending;
  assert.ok(Date.now() - start < 1_000, "sleep must resolve once the signal aborts");
});
