import { spawn } from "node:child_process";
const MARKER = "<!-- quorum:exploration -->";
const MAX_BODY = 60_000;

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

  const existing = await findExistingComment(repo, pr);
  const payload = JSON.stringify({ body });
  if (existing) {
    await gh(["api", "-X", "PATCH", `repos/${repo}/issues/comments/${existing}`, "--input", "-"], payload);
    return { action: "updated", commentId: existing, body };
  }

  const raw = await gh(["api", "-X", "POST", `repos/${repo}/issues/${pr}/comments`, "--input", "-"], payload);
  const parsed = JSON.parse(raw) as { id?: number };
  return { action: "created", commentId: parsed.id, body };
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

async function findExistingComment(repo: string, pr: string): Promise<number | undefined> {
  const raw = await gh([
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

async function gh(args: string[], input?: string): Promise<string> {
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
