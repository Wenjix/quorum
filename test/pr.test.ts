import assert from "node:assert/strict";
import test from "node:test";
import { parsePullRequestRef, repoSlug } from "../src/pr.js";

test("parsePullRequestRef accepts GitHub pull request URLs", () => {
  assert.deepEqual(parsePullRequestRef("https://github.com/Wenjix/node-to-self/pull/12"), {
    repo: "Wenjix/node-to-self",
    pr: "12",
    url: "https://github.com/Wenjix/node-to-self/pull/12",
  });
});

test("parsePullRequestRef accepts owner/repo#number shorthand", () => {
  assert.deepEqual(parsePullRequestRef("Wenjix/node-to-self#12"), {
    repo: "Wenjix/node-to-self",
    pr: "12",
    url: "https://github.com/Wenjix/node-to-self/pull/12",
  });
});

test("parsePullRequestRef rejects non-PR input", () => {
  assert.throws(() => parsePullRequestRef("https://github.com/Wenjix/node-to-self"), /Expected/);
});

test("repoSlug makes a filesystem-safe repo key", () => {
  assert.equal(repoSlug("Wenjix/node-to-self"), "Wenjix-node-to-self");
});
