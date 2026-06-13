import { spawn } from "node:child_process";

const MARKER = "<!-- quorum:exploration -->";
const MAX_BODY = 60_000;

// ---- GitHub REST helpers ----

function token(): string | undefined {
  return process.env.GITHUB_TOKEN || process.env.GH_TOKEN || undefined;
}

function authHeader(): Record<string, string> {
  const tok = token();
  if (!tok) return {};
  return { Authorization: `Bearer ${tok}` };
}

interface GitHubComment {
  id: number;
  body?: string;
}

async function apiRequest<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const tok = token();
  const url = path.startsWith("https://") ? path : `https://api.github.com/${path}`;
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    ...authHeader(),
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  const response = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`GitHub API ${method} ${path} failed (${response.status}): ${text}`);
  }
  return response.json() as Promise<T>;
}

async function apiPaginate<T extends { id: number }>(
  path: string,
): Promise<T[]> {
  const results: T[] = [];
  let page = 1;
  while (true) {
    const paginatedPath = `${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`;
    const tok = token();
    const url = `https://api.github.com/${paginatedPath}`;
    const headers: Record<string, string> = {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...authHeader(),
    };

    const response = await fetch(url, { headers });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`GitHub API GET ${paginatedPath} failed (${response.status}): ${text}`);
    }
    const pageData = (await response.json()) as T[];
    results.push(...pageData);
    if (pageData.length < 100) break;
    page++;
  }
  return results;
}

// ---- PR diff ----

/**
 * Fetch the unified diff for a PR. Uses the REST diff media type when a token is
 * available, otherwise falls back to `gh pr diff`.
 */
export async function fetchPrDiff(repo: string, pr: string): Promise<string> {
  if (token()) {
    const url = `https://api.github.com/repos/${repo}/pulls/${pr}`;
    const response = await fetch(url, {
      headers: {
        Accept: "application/vnd.github.diff",
        "X-GitHub-Api-Version": "2022-11-28",
        ...authHeader(),
      },
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(
        `GitHub API GET repos/${repo}/pulls/${pr} (diff) failed (${response.status}): ${text}`,
      );
    }
    return response.text();
  }
  return runGh(["pr", "diff", pr, "--repo", repo]);
}

// ---- Exploration comment upsert ----

export interface UpsertResult {
  action: "created" | "updated" | "dry-run";
  commentId?: number;
  body: string;
}

export async function upsertExplorationComment(
  repo: string,
  pr: string,
  markdown: string,
  options: { dryRun?: boolean } = {},
): Promise<UpsertResult> {
  const body = withMarker(repo, pr, markdown);
  if (options.dryRun) return { action: "dry-run", body };

  if (token()) {
    return upsertViaApi(repo, pr, body);
  }
  return upsertViaGh(repo, pr, body);
}

async function upsertViaApi(
  repo: string,
  pr: string,
  body: string,
): Promise<UpsertResult> {
  const existing = await findExistingCommentApi(repo, pr);
  if (existing) {
    await apiRequest<GitHubComment>(
      "PATCH",
      `repos/${repo}/issues/comments/${existing}`,
      { body },
    );
    return { action: "updated", commentId: existing, body };
  }

  const created = await apiRequest<GitHubComment>(
    "POST",
    `repos/${repo}/issues/${pr}/comments`,
    { body },
  );
  return { action: "created", commentId: created.id, body };
}

async function findExistingCommentApi(
  repo: string,
  pr: string,
): Promise<number | undefined> {
  const comments = await apiPaginate<GitHubComment>(
    `repos/${repo}/issues/${pr}/comments`,
  );
  for (const comment of comments) {
    if ((comment.body ?? "").includes("quorum:exploration")) {
      return comment.id;
    }
  }
  return undefined;
}

async function upsertViaGh(
  repo: string,
  pr: string,
  body: string,
): Promise<UpsertResult> {
  const existing = await findExistingCommentGh(repo, pr);
  const payload = JSON.stringify({ body });
  if (existing) {
    await runGh(["api", "-X", "PATCH", `repos/${repo}/issues/comments/${existing}`, "--input", "-"], payload);
    return { action: "updated", commentId: existing, body };
  }

  const raw = await runGh(["api", "-X", "POST", `repos/${repo}/issues/${pr}/comments`, "--input", "-"], payload);
  const parsed = JSON.parse(raw) as { id?: number };
  return { action: "created", commentId: parsed.id, body };
}

async function findExistingCommentGh(repo: string, pr: string): Promise<number | undefined> {
  const raw = await runGh([
    "api",
    `repos/${repo}/issues/${pr}/comments`,
    "--paginate",
    "--jq",
    '.[] | {id: .id, hit: (.body | contains("quorum:exploration"))} | @json',
  ]);
  for (const line of raw.split("\n")) {
    if (!line.trim()) continue;
    const obj = JSON.parse(line) as { id?: number; hit?: boolean };
    if (obj.hit) return obj.id;
  }
  return undefined;
}

export function withMarker(repo: string, pr: string, markdown: string): string {
  let body = [MARKER, markdown, `<sub>Quorum exploration comment for ${repo}#${pr}</sub>`].join(
    "\n\n",
  );
  if (body.length <= MAX_BODY) return body;
  body = [
    MARKER,
    markdown.slice(0, MAX_BODY - 400),
    "",
    "<sub>Report truncated to fit the GitHub comment limit. See local .quorum artifacts for the full output.</sub>",
  ].join("\n");
  return body;
}

// ---- gh CLI fallback (kept for synthesis.ts compatibility) ----

export async function runGh(args: string[], input?: string): Promise<string> {
  return await new Promise((resolve, reject) => {
    const child = spawn("gh", args, {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) {
        resolve(stdout);
      } else {
        reject(new Error(stderr.trim() || `gh exited with code ${code}`));
      }
    });
    if (input !== undefined) child.stdin.write(input);
    child.stdin.end();
  });
}
