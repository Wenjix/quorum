import { parseScoredClusters } from "./clusters.js";
import { runGh } from "./github.js";
import type { ScoredClustersDoc } from "./types.js";

export function extractScoredClustersFromCommentBody(body: string): ScoredClustersDoc {
  const fences = body.matchAll(/```json\s*([\s\S]*?)```/gi);
  for (const fence of fences) {
    try {
      return parseScoredClusters(JSON.parse(fence[1]));
    } catch {
      // Keep looking: the comment may contain other JSON snippets.
    }
  }
  throw new Error("No valid clusters.scored.json block found in the Quorum synthesis comment.");
}

export async function recoverScoredClustersFromPullRequest(
  repo: string,
  pr: string,
): Promise<ScoredClustersDoc> {
  const raw = await runGh([
    "api",
    `repos/${repo}/issues/${pr}/comments`,
    "--paginate",
    "--jq",
    '.[] | select(.body | contains("quorum:synthesis")) | .body | @json',
  ]);

  const bodies = raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line) as string);

  for (const body of bodies.reverse()) {
    try {
      return extractScoredClustersFromCommentBody(body);
    } catch {
      // Try older synthesis comments if multiple exist.
    }
  }

  throw new Error(
    [
      `No Quorum synthesis with embedded clusters.scored.json was found on ${repo}#${pr}.`,
      "Run the Quorum skill first, or pass --scored path/to/clusters.scored.json.",
    ].join(" "),
  );
}
