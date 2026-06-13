import { Agent } from "@cursor/sdk";
import type { Run, SDKAgent, SDKMessage } from "@cursor/sdk";
import { withRetry } from "./retry.js";
import type { TaskExecutionInput, TaskExecutionResult, TaskRunnerAdapter } from "./types.js";

export class CursorCloudAdapter implements TaskRunnerAdapter {
  constructor(private options: { maxRetries?: number } = {}) {}

  async runTask(input: TaskExecutionInput): Promise<TaskExecutionResult> {
    return withRetry(() => this.runOnce(input), {
      maxRetries: this.options.maxRetries,
      signal: input.signal,
      label: `Task ${input.task.id}`,
    });
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
      throw describeCursorError(error);
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

/**
 * Turn Cursor's opaque simultaneous-agent plan-limit error into an actionable
 * message. This fires when more Cloud Agents are launched at once than the
 * plan allows; the fix is lower concurrency, not a different API key.
 */
export function describeCursorError(error: unknown): Error {
  const message = error instanceof Error ? error.message : String(error);
  if (/simultaneous|upgrade to ultra|more cloud agents/i.test(message)) {
    return new Error(
      "Cursor rejected a concurrent Cloud Agent launch — your plan limits how many " +
        "Cloud Agents run at once. Lower --concurrency (the cursor default is 1) or " +
        `upgrade your Cursor plan. Original error: ${message}`,
    );
  }
  return error instanceof Error ? error : new Error(message);
}

function textFromSdkMessage(event: SDKMessage): string {
  if (event.type !== "assistant") return "";
  return event.message.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("");
}
