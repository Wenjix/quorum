import assert from "node:assert/strict";
import test from "node:test";
import { describeCursorError } from "../src/cursor-cloud-adapter.js";

const PLAN_LIMIT_MESSAGE =
  "[validation_error] Upgrade to Ultra for more Cloud Agents: You've reached " +
  "the limit for your current plan. Upgrade to Ultra to run more Cloud Agents " +
  "simultaneously.";

test("describeCursorError rewrites the simultaneous-agent plan-limit error", () => {
  const result = describeCursorError(new Error(PLAN_LIMIT_MESSAGE));
  assert.ok(result instanceof Error);
  // Points the user at the real fix instead of the opaque upstream message.
  assert.match(result.message, /--concurrency/);
  assert.match(result.message, /plan limits how many/i);
  // No stale hardcoded default (the runner default has changed before).
  assert.doesNotMatch(result.message, /default is \d/);
  // Preserves the original text for debugging.
  assert.match(result.message, /Original error:/);
  assert.ok(result.message.includes("simultaneously"));
});

test("describeCursorError leaves unrelated errors unchanged", () => {
  const original = new Error("Task aborted.");
  const result = describeCursorError(original);
  assert.equal(result, original);
  assert.doesNotMatch(result.message, /--concurrency/);
});

test("describeCursorError wraps non-Error values", () => {
  const result = describeCursorError("network blip");
  assert.ok(result instanceof Error);
  assert.equal(result.message, "network blip");
});
