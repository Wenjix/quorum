import { Agent } from "@cursor/sdk";
import type { Run, SDKAgent, SDKMessage } from "@cursor/sdk";
import type { TaskExecutionInput, TaskExecutionResult, TaskRunnerAdapter } from "./types.js";

const MAX_RETRIES = 3;
const RETRY_BACKOFF_MS = [1_000, 4_000, 16_000];

function retryableError(error: unknown): boolean {
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    if (msg.includes("rate") || msg.includes("throttle") || msg.includes("capacity")) return true;
    if (msg.includes("network") || msg.includes("timeout") || msg.includes("econnrefused")) return true;
    if (msg.includes("5") && (msg.includes("status") || msg.includes("500") || msg.includes("502") || msg.includes("503") || msg.includes("504"))) return true;
    if (msg.includes("429")) return true;
  }
  return false;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class CursorCloudAdapter implements TaskRunnerAdapter {
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
    const started = Date.now();
    let agent: SDKAgent | undefined;
    let run: Run | undefined;

    const abortHandler = (): void => {
      if (run?.supports("cancel")) {
        void run.cancel().catch(() => undefined);
      }
    };
    input.signal.addEventListener("abort", abortHandler);

    try {
      if (input.signal.aborted) {
        throw new Error("Task aborted before agent creation.");
      }
      agent = await Agent.create({
        apiKey: input.apiKey,
        name: `Quorum ${input.task.id}`,
        model: { id: input.model },
        cloud: {
          repos: [
            {
              url: input.repoUrl,
              ...(input.prUrl ? { prUrl: input.prUrl } : {}),
            },
          ],
          autoCreatePR: false,
          workOnCurrentBranch: false,
          skipReviewerRequest: true,
        },
      });

      run = await agent.send(input.prompt, {
        mode: "agent",
        idempotencyKey: input.idempotencyKey,
      });

      let streamedText = "";
      if (input.stream && run.supports("stream")) {
        for await (const event of run.stream()) {
          if (input.signal.aborted) {
            await cancelBestEffort(run);
            throw new Error("Task aborted.");
          }
          streamedText += textFromSdkMessage(event);
        }
      }

      if (!run.supports("wait")) {
        return {
          status: run.status === "finished" ? "finished" : "error",
          resultText: run.result ?? streamedText,
          durationMs: Date.now() - started,
          agentId: agent.agentId,
          runId: run.id,
          requestId: run.requestId,
        };
      }

      const result = await run.wait();
      return {
        status: result.status === "finished" ? "finished" : result.status,
        resultText: result.result ?? streamedText,
        durationMs: result.durationMs ?? Date.now() - started,
        agentId: agent.agentId,
        runId: run.id,
        requestId: result.requestId ?? run.requestId,
      };
    } catch (error) {
      if (input.signal.aborted && run) {
        await cancelBestEffort(run);
      }
      throw error;
    } finally {
      input.signal.removeEventListener("abort", abortHandler);
      if (agent) {
        await agent[Symbol.asyncDispose]().catch(() => undefined);
      }
    }
  }
}

async function cancelBestEffort(run: Run): Promise<void> {
  if (!run.supports("cancel")) return;
  await run.cancel().catch(() => undefined);
}

function textFromSdkMessage(event: SDKMessage): string {
  if (event.type !== "assistant") return "";
  return event.message.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("");
}
