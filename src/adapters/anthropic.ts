import { withRetry } from "../retry.js";
import type { TaskExecutionInput, TaskExecutionResult, TaskRunnerAdapter } from "../types.js";

const API_BASE = process.env.ANTHROPIC_BASE_URL || "https://api.anthropic.com";

/**
 * Task runner that calls the Anthropic Messages API directly.
 * Set ANTHROPIC_API_KEY to use this adapter.
 */
export class AnthropicAdapter implements TaskRunnerAdapter {
  constructor(private options: { maxRetries?: number } = {}) {}

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

    // Parse validation is owned by the runner (see applyResult): a finished-but-
    // unparseable response is recorded as a parseError downstream, not a task
    // error. The adapter only reports that the model produced a response.
    return {
      status: "finished",
      resultText: responseText,
      durationMs: Date.now() - started,
    };
  }
}
