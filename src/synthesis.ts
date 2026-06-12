import { parseScoredClusters } from "./clusters.js";
import { runGh } from "./github.js";
import type { ScoredClustersDoc } from "./types.js";

interface GitHubSynthesisComment {
  id: number;
  body?: string;
}

function token(): string | undefined {
  return process.env.GITHUB_TOKEN || process.env.GH_TOKEN || undefined;
}

function authHeader(): Record<string, string> {
  const tok = token();
  if (!tok) return {};
  return { Authorization: `Bearer ${tok}` };
}

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
  if (token()) {
    return recoverViaApi(repo, pr);
  }
  return recoverViaGh(repo, pr);
}

async function recoverViaApi(
  repo: string,
  pr: string,
): Promise<ScoredClustersDoc> {
  const bodies: string[] = [];
  let page = 1;
  while (true) {
    const url = `https://api.github.com/repos/${repo}/issues/${pr}/comments?per_page=100&page=${page}`;
    const headers: Record<string, string> = {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...authHeader(),
    };

    const response = await fetch(url, { headers });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`GitHub API GET repos/${repo}/issues/${pr}/comments failed (${response.status}): ${text}`);
    }
    const comments = (await response.json()) as GitHubSynthesisComment[];
    for (const comment of comments) {
      if ((comment.body ?? "").includes("quorum:synthesis")) {
        bodies.push(comment.body!);
      }
    }
    if (comments.length < 100) break;
    page++;
  }

  return processBodies(bodies, repo, pr);
}

async function recoverViaGh(
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

  return processBodies(bodies, repo, pr);
}

function processBodies(bodies: string[], repo: string, pr: string): ScoredClustersDoc {
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
