import { parseScoredClusters } from "./clusters.js";
import { runGh } from "./github.js";
import type { ScoredClustersDoc } from "./types.js";

interface GitHubSynthesisComment {
  id: number;
  body?: string;
  user?: { login?: string } | null;
  author_association?: string;
}

/** GitHub `author_association` values that vouch for a comment's author. */
const TRUSTED_ASSOCIATIONS = new Set(["OWNER", "MEMBER", "COLLABORATOR"]);

/**
 * Anyone can comment on a public pull request, so the marker alone proves
 * nothing: an outsider could plant a forged synthesis carrying fabricated
 * clusters. A marker comment is trusted only when it was written by the
 * authenticated account (`login`) or by a repository OWNER, MEMBER or
 * COLLABORATOR. Mirrors `_is_trusted` in scripts/quorum_github.py.
 */
export function isTrustedComment(
  comment: { user?: { login?: string } | null; author_association?: string },
  login: string | null,
): boolean {
  if (comment.author_association !== undefined && TRUSTED_ASSOCIATIONS.has(comment.author_association)) {
    return true;
  }
  const author = comment.user?.login;
  return login !== null && typeof author === "string" && author !== "" && author === login;
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
  // Anchored to line starts so a backtick run inside a JSON string (a note, a
  // title) cannot close the fence early: it ends at the next line that is
  // exactly ```.
  const fences = body.matchAll(/^```json[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*$/gim);
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
  const candidates: GitHubSynthesisComment[] = [];
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
        candidates.push(comment);
      }
    }
    if (comments.length < 100) break;
    page++;
  }

  const login = candidates.length > 0 ? await authenticatedLoginViaApi() : null;
  return processBodies(trustedBodies(candidates, login), repo, pr);
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
    '.[] | select((.body // "") | contains("quorum:synthesis")) | {id: .id, body: .body, user: {login: .user.login}, author_association: .author_association} | @json',
  ]);

  const candidates = raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line) as GitHubSynthesisComment);

  const login = candidates.length > 0 ? await authenticatedLoginViaGh() : null;
  return processBodies(trustedBodies(candidates, login), repo, pr);
}

function trustedBodies(candidates: GitHubSynthesisComment[], login: string | null): string[] {
  return candidates
    .filter((comment) => isTrustedComment(comment, login))
    .map((comment) => comment.body ?? "");
}

/** Best-effort: some tokens (GitHub App installation tokens) cannot read `GET /user`. */
async function authenticatedLoginViaApi(): Promise<string | null> {
  try {
    const response = await fetch("https://api.github.com/user", {
      headers: {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        ...authHeader(),
      },
    });
    if (!response.ok) return null;
    return loginFrom(((await response.json()) as { login?: unknown }).login);
  } catch {
    return null;
  }
}

async function authenticatedLoginViaGh(): Promise<string | null> {
  try {
    return loginFrom((await runGh(["api", "user", "--jq", ".login"])).trim());
  } catch {
    return null;
  }
}

function loginFrom(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
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
      `No trusted Quorum synthesis with embedded clusters.scored.json was found on ${repo}#${pr}`,
      "(only comments by the authenticated account or a repository OWNER, MEMBER or COLLABORATOR count).",
      "Run the Quorum skill first, or pass --scored path/to/clusters.scored.json.",
    ].join(" "),
  );
}
