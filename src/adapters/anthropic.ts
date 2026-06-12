import { extractMarkedJson } from "../json-result.js";
import type { TaskExecutionInput, TaskExecutionResult, TaskRunnerAdapter } from "../types.js";

const MAX_RETRIES = 3;
const RETRY_BACKOFF_MS = [1_000, 4_000, 16_000];

function retryableError(error: unknown): boolean {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    if (msg.includes("rate") || msg.includes("throttle") || msg.includes("capacity")) return true;
    if (msg.includes("network") || msg.includes("timeout") || msg.includes("econnrefused")) return true;
    if (msg.includes("5") && (msg.includes("status") || msg.includes("500") || msg.includes("502") || msg.includes("503") || msg.includes("504"))) return true;
    if (msg.includes("429")) return true;
    if (msg.includes("overloaded")) return true;
  }
  return false;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const API_BASE = process.env.ANTHROPIC_BASE_URL || "https://api.anthropic.com";

/**
 * Task runner that calls the Anthropic Messages API directly.
 * Set ANTHROPIC_API_KEY to use this adapter.
 */
export class AnthropicAdapter implements TaskRunnerAdapter {
  constructor(private options: { maxRetries?: number } = {}) {}

  async runTask(input: TaskExecutionInput): Promise<TaskExecutionResult> {
    const maxRetries = this.options.maxRetries ?? MAX_RETRIES;
    let lastError: unknown;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      try {
        return await this.runOnce(input);
      } catch (error) {
        lastError = error;
        if (input.signal.aborted) break;
        if (attempt < maxRetries && retryableError(error)) {
          const delay = RETRY_BACKOFF_MS[Math.min(attempt, RETRY_BACKOFF_MS.length - 1)];
          console.error(`Task ${input.task.id} attempt ${attempt + 1} failed, retrying in ${delay}ms: ${error instanceof Error ? error.message : String(error)}`);
          await sleep(delay);
          continue;
        }
        break;
      }
    }
    throw lastError;
  }

  private async runOnce(input: TaskExecutionInput): Promise<TaskExecutionResult> {
    const apiKey = input.apiKey || process.env.ANTHROPIC_API_KEY;
    if (!apiKey) {
      throw new Error("ANTHROPIC_API_KEY is required for the Anthropic adapter.");
    }

    const started = Date.now();
    const systemPrompt = [
      "You are a read-only PR review exploration agent for Quorum.",
      "Do not suggest edits, create commits, push branches, or open pull requests.",
      "Return a concise human-readable explanation, then end with exactly one fenced JSON block as specified.",
    ].join("\n");

    let responseText = "";
    let assistantOutput = "";

    try {
      const response = await fetch(`${API_BASE}/v1/messages`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": apiKey,
          "anthropic-version": "2023-06-01",
          "anthropic-dangerous-direct-browser-access": "true",
        },
        signal: input.signal,
        body: JSON.stringify({
          model: input.model,
          max_tokens: 4096,
          system: systemPrompt,
          messages: [{ role: "user", content: input.prompt }],
        }),
      });

      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(`Anthropic API error (${response.status}): ${text}`);
      }

      const body = (await response.json()) as {
        content: Array<{ type: string; text?: string }>;
        stop_reason?: string;
        id?: string;
      };

      for (const block of body.content) {
        if (block.type === "text" && block.text) {
          assistantOutput += block.text;
        }
      }
      responseText = assistantOutput;

      if (body.stop_reason === "max_tokens") {
        console.error(`Task ${input.task.id}: response hit max_tokens limit`);
      }
    } catch (error) {
      if (input.signal.aborted) {
        throw new Error("Task aborted.");
      }
      throw error;
    }

    const parsed = extractMarkedJson(responseText);
    const status = parsed.value !== undefined ? "finished" as const : "finished" as const;
    return {
      status,
      resultText: responseText,
      durationMs: Date.now() - started,
      // Anthropic doesn't have agent/run IDs; use the request ID if available
    };
  }
}
