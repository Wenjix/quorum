import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { extractScoredClustersFromCommentBody, isTrustedComment } from "../src/synthesis.js";
import { sampleScoredDoc, sampleTriageDoc } from "./helpers.js";

// A ledger comment exactly as scripts/quorum_render.py renders it for
// scripts/tests/fixtures/triage.ledger.json. scripts/tests/test_render.py fails
// when it falls behind the renderer or the protocol; regenerate it from the
// repository root with:
//   python3 -c "import json,sys; sys.path.insert(0,'scripts'); from quorum_ledger import LEDGER_REF; from quorum_render import load_protocol, render_comment; print(render_comment(json.load(open('scripts/tests/fixtures/triage.ledger.json')), load_protocol(LEDGER_REF)))" > test/fixtures/rendered-ledger.md
// (tests run from dist/test/, hence the two levels up).
const RENDERED_LEDGER = fileURLToPath(new URL("../../test/fixtures/rendered-ledger.md", import.meta.url));

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

test("extractScoredClustersFromCommentBody recovers the document from a python-rendered ledger", () => {
  const body = readFileSync(RENDERED_LEDGER, "utf8");
  assert.ok(body.includes("```mermaid"), "the fixture carries the brief's invariant map");
  const doc = extractScoredClustersFromCommentBody(body);
  assert.equal(doc.schema, "quorum/triage@1");
  assert.equal(doc.pr, "o/r#7");
  assert.deepEqual(
    doc.clusters.map((cluster) => cluster.cluster_id),
    ["c1", "c4", "c2", "c3"],
  );
  assert.deepEqual(doc.lanes, { agreed: ["c1", "c4"], solo: ["c2", "c3"] });
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
