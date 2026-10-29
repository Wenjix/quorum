import assert from "node:assert/strict";
import test from "node:test";
import { parseClusterIds, parseScoredClusters, selectClusters } from "../src/clusters.js";
import { sampleScoredDoc, sampleTriageDoc } from "./helpers.js";

test("selectClusters defaults to quorum >= 2", () => {
  const selected = selectClusters(sampleScoredDoc());
  assert.deepEqual(
    selected.map((cluster) => cluster.cluster_id),
    ["c1"],
  );
});

test("selectClusters accepts explicit cluster ids and max limit", () => {
  const selected = selectClusters(sampleScoredDoc(), {
    clusterIds: ["c2", "c1"],
    maxClusters: 1,
  });
  assert.equal(selected.length, 1);
  assert.equal(selected[0].cluster_id, "c1");
});

test("parseClusterIds supports repeated comma-delimited flags", () => {
  assert.deepEqual(parseClusterIds(["c1,c2", "c3"]), ["c1", "c2", "c3"]);
});

test("selectClusters uses the agreed lane when lanes are present", () => {
  const selected = selectClusters(sampleTriageDoc());
  assert.deepEqual(
    selected.map((cluster) => cluster.cluster_id),
    ["c1"],
  );
});

test("selectClusters honours an explicit --min-quorum over lanes", () => {
  const selected = selectClusters(sampleTriageDoc(), { minQuorum: 1 });
  assert.equal(selected.length, 2);
});

test("selectClusters falls back to quorum >= 2 when no cluster has a lane", () => {
  const doc = sampleScoredDoc();
  assert.ok(doc.clusters.every((cluster) => cluster.lane === undefined));
  assert.deepEqual(
    selectClusters(doc).map((cluster) => cluster.cluster_id),
    ["c1"],
  );
});

test("selectClusters lane mode ignores a quorum-2 cluster that is not in the agreed lane", () => {
  const doc = sampleTriageDoc();
  doc.clusters[0].lane = "solo";
  doc.clusters[1].lane = "agreed";
  assert.deepEqual(
    selectClusters(doc).map((cluster) => cluster.cluster_id),
    ["c2"],
  );
});

test("parseScoredClusters accepts the truncated minimal quorum/triage@1 document", () => {
  const doc = parseScoredClusters({
    schema: "quorum/triage@1",
    pr: "OWNER/REPO#1",
    head: "abc",
    totals: { findings: 3, clusters: 2, reviewer_denominator: 3 },
    lanes: { agreed: ["c1"], solo: ["c2"] },
    truncated: true,
    clusters: [
      {
        cluster_id: "c1",
        member_ids: ["bugbot-1", "devin-1"],
        members: [
          { id: "bugbot-1", comment_id: 1 },
          { id: "devin-1", comment_id: 2 },
        ],
        quorum: 2,
        reviewers: ["bugbot", "devin"],
        lane: "agreed",
        escalation: null,
        disposition: null,
      },
      {
        cluster_id: "c2",
        member_ids: ["copilot-1"],
        members: [{ id: "copilot-1", comment_id: 3 }],
        quorum: 1,
        reviewers: ["copilot"],
        lane: "solo",
        escalation: null,
        disposition: null,
      },
    ],
  });
  assert.equal(doc.schema, "quorum/triage@1");
  assert.deepEqual(
    selectClusters(doc).map((cluster) => cluster.cluster_id),
    ["c1"],
  );
});
