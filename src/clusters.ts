import type { ScoredCluster, ScoredClustersDoc } from "./types.js";

export interface ClusterSelectionOptions {
  clusterIds?: string[];
  minQuorum?: number;
  maxClusters?: number;
}

export function parseScoredClusters(raw: unknown): ScoredClustersDoc {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("clusters.scored.json must be a JSON object.");
  }
  const doc = raw as ScoredClustersDoc;
  if (!doc.totals || typeof doc.totals.reviewer_denominator !== "number") {
    throw new Error("clusters.scored.json missing totals.reviewer_denominator.");
  }
  if (!Array.isArray(doc.clusters)) {
    throw new Error("clusters.scored.json missing clusters array.");
  }
  for (const cluster of doc.clusters) {
    if (!cluster.cluster_id || !Array.isArray(cluster.member_ids)) {
      throw new Error("clusters.scored.json contains an invalid cluster.");
    }
    if (typeof cluster.quorum !== "number") {
      throw new Error(`cluster ${cluster.cluster_id} missing numeric quorum.`);
    }
  }
  return doc;
}

export function selectClusters(
  doc: ScoredClustersDoc,
  options: ClusterSelectionOptions = {},
): ScoredCluster[] {
  const minQuorum = options.minQuorum ?? 2;
  let selected: ScoredCluster[];

  if (options.clusterIds && options.clusterIds.length > 0) {
    const wanted = new Set(options.clusterIds);
    selected = doc.clusters.filter((cluster) => wanted.has(cluster.cluster_id));
    const missing = [...wanted].filter(
      (id) => !selected.some((cluster) => cluster.cluster_id === id),
    );
    if (missing.length > 0) {
      throw new Error(`Unknown cluster id(s): ${missing.join(", ")}`);
    }
  } else {
    selected = doc.clusters.filter((cluster) => cluster.quorum >= minQuorum);
  }

  selected = selected.sort((a, b) => {
    if (b.quorum !== a.quorum) return b.quorum - a.quorum;
    return severityRank(b.severity) - severityRank(a.severity);
  });

  if (options.maxClusters !== undefined) {
    selected = selected.slice(0, options.maxClusters);
  }
  return selected;
}

export function parseClusterIds(values: string[]): string[] {
  return values
    .flatMap((value) => value.split(","))
    .map((value) => value.trim())
    .filter(Boolean);
}

function severityRank(severity: string): number {
  switch (severity) {
    case "critical":
      return 3;
    case "major":
      return 2;
    case "minor":
      return 1;
    case "nit":
      return 0;
    default:
      return -1;
  }
}
