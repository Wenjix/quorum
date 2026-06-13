import assert from "node:assert/strict";
import test from "node:test";
import { buildDiffPrompt } from "../src/adapters/anthropic.js";

test("buildDiffPrompt embeds the diff as code context", () => {
  const out = buildDiffPrompt("Investigate the bug.", "diff --git a/x.ts b/x.ts\n+const bug = 1;");
  assert.match(out, /<pr_diff>/);
  assert.match(out, /diff --git a\/x\.ts/);
  assert.match(out, /Investigate the bug\./);
  assert.doesNotMatch(out, /could not be retrieved/);
});

test("buildDiffPrompt notes when no diff is available", () => {
  const out = buildDiffPrompt("Investigate the bug.", "   ");
  assert.match(out, /could not be retrieved/);
  assert.match(out, /Investigate the bug\./);
  assert.doesNotMatch(out, /<pr_diff>/);
});

test("buildDiffPrompt truncates oversized diffs", () => {
  const big = "x".repeat(300_000);
  const out = buildDiffPrompt("prompt", big);
  assert.match(out, /diff truncated/);
  assert.ok(out.length < big.length + 2_000, "oversized diff should be capped");
});

test("buildDiffPrompt keeps blank-line separators around the diff block", () => {
  const out = buildDiffPrompt("the task", "diff --git a/x b/x\n+y");
  assert.match(out, /\n\n<pr_diff>/); // blank line before the diff block
  assert.match(out, /<\/pr_diff>\n\n---/); // blank line after it, before the rule
  assert.match(out, /---\n\nthe task/); // blank line before the prompt
});
