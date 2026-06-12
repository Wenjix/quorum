import assert from "node:assert/strict";
import test from "node:test";
import { parseClusterIds, selectClusters } from "../src/clusters.js";
import { sampleScoredDoc } from "./helpers.js";

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
