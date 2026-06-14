import { fetchPrDiff } from "../github.js";
import { withRetry } from "../retry.js";
import type { TaskExecutionInput, TaskExecutionResult, TaskRunnerAdapter } from "../types.js";

const API_BASE = process.env.ANTHROPIC_BASE_URL || "https://api.anthropic.com";

/**
 * Upper bound on injected diff size (chars). Claude handles far more, but a
 * giant diff wastes tokens; beyond this we truncate and tell the model.
 */
const DIFF_CHAR_CAP = 200_000;

const SYSTEM_PROMPT = [
  "You are a read-only PR review exploration agent for Quorum.",
  "You are given the pull request's unified diff as your view of the code; the",
  "repository is not otherwise available. Base your analysis on that diff and the",
  "finding details, and say so explicitly when the relevant code is not shown.",
  "Do not suggest edits, create commits, push branches, or open pull requests.",
  "Return a concise human-readable explanation, then end with exactly one fenced",
  "JSON block as specified.",
].join("\n");

/**
 * Compose the user turn: the PR diff (capped) as code context, then the task
 * prompt. Exported for testing.
 */
export function buildDiffPrompt(prompt: string, diff: string): string {
  const trimmed = diff.trim();
  if (!trimmed) {
    return [
      "NOTE: the pull request diff could not be retrieved, so no code context is",
      "available. Analyze from the finding details below and flag where you lack",
      "evidence rather than guessing.",
      "",
      prompt,
    ].join("\n");
  }
  const truncated = trimmed.length > DIFF_CHAR_CAP;
  const body = truncated ? `${trimmed.slice(0, DIFF_CHAR_CAP)}\n... (diff truncated)` : trimmed;
  return [
    "The pull request's unified diff is provided below as your view of the code.",
    ...(truncated ? ["It was truncated to fit; some changes are omitted."] : []),
    "",
    "<pr_diff>",
    body,
    "</pr_diff>",
    "",
    "---",
    "",
    prompt,
  ].join("\n");
}

/**
 * Task runner that calls the Anthropic Messages API, injecting the PR diff so the
 * model can reason about the actual code. Set ANTHROPIC_API_KEY to use it.
 */
export class AnthropicAdapter implements TaskRunnerAdapter {
  constructor(private options: { maxRetries?: number } = {}) {}

  // Diff fetched once per (repo, pr) and shared across a run's tasks (the promise
  // is memoized so concurrent first calls don't each fetch). Keyed by repo#pr so
  // reusing one adapter across PRs can't inject the wrong diff.
  private diffCache = new Map<string, Promise<string>>();

  private loadDiff(repo: string, pr: string): Promise<string> {
    const key = `${repo}#${pr}`;
    let cached = this.diffCache.get(key);
    if (!cached) {
      // Retry transient fetch failures (5xx/429/network) before degrading, the
      // same way the Messages API call is retried.
      cached = withRetry(() => fetchPrDiff(repo, pr), {
        maxRetries: this.options.maxRetries,
        label: `PR diff ${key}`,
      }).catch((error) => {
        const detail = error instanceof Error ? error.message : String(error);
        console.error(
          `Could not fetch the PR diff for ${key}; Anthropic exploration will run ` +
            `without code context. Set GITHUB_TOKEN or authenticate gh. (${detail})`,
        );
        return "";
      });
      this.diffCache.set(key, cached);
    }
    return cached;
  }

  async runTask(input: TaskExecutionInput): Promise<TaskExecutionResult> {
    return withRetry(() => this.runOnce(input), {
      maxRetries: this.options.maxRetries,
      signal: input.signal,
      label: `Task ${input.task.id}`,
    });
  }

  private async runOnce(input: TaskExecutionInput): Promise<TaskExecutionResult> {
    const apiKey = input.apiKey || process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      throw new Error("ANTHROPIC_API_KEY is required for the Anthropic adapter.");
    }

    const started = Date.now();
    const diff = await this.loadDiff(input.repo, input.pr);
    const userPrompt = buildDiffPrompt(input.prompt, diff);

    let responseText = "";

    try {
      const response = await fetch(`${API_BASE}/v1/messages`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": apiKey,
          "anthropic-version": "2023-06-01",
        },
        signal: input.signal,
        body: JSON.stringify({
          model: input.model,
          max_tokens: 8192,
          system: SYSTEM_PROMPT,
          messages: [{ role: "user", content: userPrompt }],
        }),
      });

      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(`Anthropic API error (${response.status}): ${text}`);
      }

      const body = (await response.json()) as {
        content?: Array<{ type: string; text?: string }>;
        stop_reason?: string;
      };

      // Guard against a malformed success response that omits/nulls content;
      // an empty result is recorded downstream as a parseError, not a throw.
      const blocks = Array.isArray(body.content) ? body.content : [];
      for (const block of blocks) {
        if (block.type === "text" && block.text) {
          responseText += block.text;
        }
      }

      if (body.stop_reason === "max_tokens") {
        console.error(`Task ${input.task.id}: response hit max_tokens limit`);
      }
    } catch (error) {
      if (input.signal.aborted) {
        throw new Error("Task aborted.");
      }
      throw error;
    }

    // Parse validation is owned by the runner (see applyResult): a finished-but-
    // unparseable response is recorded as a parseError downstream, not a task error.
    return {
      status: "finished",
      resultText: responseText,
      durationMs: Date.now() - started,
    };
  }
}
