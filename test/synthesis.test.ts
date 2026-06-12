import assert from "node:assert/strict";
import test from "node:test";
import { extractScoredClustersFromCommentBody } from "../src/synthesis.js";
import { sampleScoredDoc } from "./helpers.js";

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
