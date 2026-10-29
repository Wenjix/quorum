import assert from "node:assert/strict";
import test from "node:test";
import { extractScoredClustersFromCommentBody, isTrustedComment } from "../src/synthesis.js";
import { sampleScoredDoc, sampleTriageDoc } from "./helpers.js";

test("extractScoredClustersFromCommentBody finds embedded clusters.scored.json", () => {
  const sample = sampleScoredDoc();
  const body = [
    "<!-- quorum:synthesis -->",
    "details",
    "```json",
    JSON.stringify(sample, null, 2),
    "```",
  ].join("\n");

  const extracted = extractScoredClustersFromCommentBody(body);
  assert.equal(extracted.totals.findings, sample.totals.findings);
  assert.equal(extracted.clusters[0].cluster_id, "c1");
});

test("extractScoredClustersFromCommentBody reports missing JSON clearly", () => {
  assert.throws(
    () => extractScoredClustersFromCommentBody("<!-- quorum:synthesis -->\nno json here"),
    /No valid clusters\.scored\.json/,
  );
});

test("extractScoredClustersFromCommentBody recovers quorum/triage@1 from a ledger comment", () => {
  const body = [
    "<!-- quorum:synthesis -->",
    "## 🤝 Quorum · triage brief",
    "```mermaid",
    "flowchart LR",
    "```",
    "<details>",
    "```json",
    JSON.stringify(sampleTriageDoc()),
    "```",
    "</details>",
  ].join("\n");
  const doc = extractScoredClustersFromCommentBody(body);
  assert.equal(doc.schema, "quorum/triage@1");
  assert.equal(doc.clusters[0].lane, "agreed");
});

test("extractScoredClustersFromCommentBody survives a backtick fence inside a JSON string", () => {
  const triage = sampleTriageDoc();
  triage.clusters[0].canonical_description = "inline ``` fence";
  const json = JSON.stringify(triage);
  assert.ok(json.includes('"inline ``` fence"'));
  const body = [
    "<!-- quorum:synthesis -->",
    "```mermaid",
    "flowchart LR",
    "```",
    "<details>",
    "```json",
    json,
    "```",
    "</details>",
  ].join("\n");
  const doc = extractScoredClustersFromCommentBody(body);
  assert.equal(doc.schema, "quorum/triage@1");
  assert.equal(doc.clusters[0].canonical_description, "inline ``` fence");
});

test("isTrustedComment rejects an outsider with association NONE", () => {
  assert.equal(
    isTrustedComment({ user: { login: "mallory" }, author_association: "NONE" }, "quorum-bot"),
    false,
  );
});

test("isTrustedComment accepts the authenticated login even with association NONE", () => {
  assert.equal(
    isTrustedComment({ user: { login: "quorum-bot" }, author_association: "NONE" }, "quorum-bot"),
    true,
  );
});

test("isTrustedComment accepts OWNER, MEMBER and COLLABORATOR under another login", () => {
  for (const association of ["OWNER", "MEMBER", "COLLABORATOR"]) {
    assert.equal(
      isTrustedComment({ user: { login: "someone" }, author_association: association }, "quorum-bot"),
      true,
      association,
    );
  }
});

test("isTrustedComment rejects a missing user with association NONE", () => {
  assert.equal(isTrustedComment({ user: null, author_association: "NONE" }, "quorum-bot"), false);
  assert.equal(isTrustedComment({ author_association: "NONE" }, null), false);
});

test("isTrustedComment does not trust a missing user just because no login is known", () => {
  assert.equal(isTrustedComment({ user: null }, null), false);
  assert.equal(isTrustedComment({ user: { login: "x" } }, null), false);
});
