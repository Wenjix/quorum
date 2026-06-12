import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { canvasPathFromOutDir, writeCanvas } from "./canvas.js";
import { computeRanks, createModelResolver } from "./dag.js";
import { extractMarkedJson } from "./json-result.js";
import type {
  Dag,
  DagTask,
  RunnerContext,
  RunState,
  TaskExecutionResult,
  TaskRunnerAdapter,
  TaskState,
} from "./types.js";

const UPSTREAM_SNIPPET_CAP = 2_000;

export function initialRunState(dag: Dag): RunState {
  const modelFor = createModelResolver(dag.models);
  return {
    title: dag.title,
    startedAt: Date.now(),
    tasks: dag.tasks.map((task) => ({
      ...task,
      status: "PENDING",
      model: modelFor(task.complexity),
    })),
  };
}

export async function runDag(
  dag: Dag,
  context: RunnerContext,
  adapter: TaskRunnerAdapter,
): Promise<RunState> {
  await mkdir(context.outDir, { recursive: true });
  const ranks = computeRanks(dag);
  const state = initialRunState(dag);
  const stateById = new Map(state.tasks.map((task) => [task.id, task]));
  await writeState(context.outDir, state, context.canvasPath);

  for (const rank of ranks) {
    await mapWithConcurrency(rank, context.concurrency, async (task) => {
      const taskState = stateById.get(task.id)!;
      const failedDeps = task.depends_on.filter((depId) => {
        const dep = stateById.get(depId);
        return dep?.status === "ERROR" || dep?.status === "SKIPPED";
      });
      if (failedDeps.length > 0) {
        taskState.status = "SKIPPED";
        taskState.finishedAt = Date.now();
        taskState.durationMs = 0;
        taskState.errorMessage = `Skipped because upstream task(s) failed: ${failedDeps.join(", ")}`;
        await writeState(context.outDir, state, context.canvasPath);
        return;
      }

      await runOneTask(task, taskState, stateById, state, context, adapter);
      await writeState(context.outDir, state, context.canvasPath);
    });
  }

  state.finishedAt = Date.now();
  const failed = state.tasks.filter((task) => task.status === "ERROR" || task.status === "SKIPPED");
  state.runOutcome = failed.length > 0 ? "FAILED" : "SUCCESS";
  state.runMessage =
    failed.length > 0
      ? `Some tasks failed or were skipped: ${failed.map((task) => task.id).join(", ")}`
      : "All tasks finished.";
  await writeState(context.outDir, state, context.canvasPath);
  return state;
}

export async function writeState(
  outDir: string,
  state: RunState,
  canvasPath?: string | false,
): Promise<void> {
  await mkdir(outDir, { recursive: true });
  await writeFile(join(outDir, "state.json"), JSON.stringify(state, null, 2), "utf8");
  if (canvasPath !== false) {
    await writeCanvas(canvasPath ?? canvasPathFromOutDir(outDir), state);
  }
}

async function runOneTask(
  task: DagTask,
  taskState: TaskState,
  stateById: Map<string, TaskState>,
  state: RunState,
  context: RunnerContext,
  adapter: TaskRunnerAdapter,
): Promise<void> {
  const startedAt = Date.now();
  taskState.status = "RUNNING";
  taskState.startedAt = startedAt;
  await writeState(context.outDir, state, context.canvasPath);

  const controller = new AbortController();
  const prompt = stitchPrompt(task, stateById);
  const model = taskState.model;
  try {
    const result = await withTimeout(
      adapter.runTask({
        task,
        prompt,
        model,
        repoUrl: context.repoUrl,
        prUrl: context.prUrl,
        apiKey: context.apiKey,
        timeoutMs: context.taskTimeoutMs,
        stream: context.stream,
        idempotencyKey: `${context.repo}#${context.pr}:${task.id}`,
        signal: controller.signal,
      }),
      context.taskTimeoutMs,
      () => controller.abort(),
    );
    applyResult(taskState, result);
  } catch (error) {
    taskState.status = "ERROR";
    taskState.errorMessage = error instanceof Error ? error.message : String(error);
  } finally {
    taskState.finishedAt = Date.now();
    taskState.durationMs = taskState.finishedAt - startedAt;
  }
}

function applyResult(taskState: TaskState, result: TaskExecutionResult): void {
  taskState.resultText = result.resultText;
  taskState.agentId = result.agentId;
  taskState.runId = result.runId;
  taskState.requestId = result.requestId;
  taskState.durationMs = result.durationMs;
  taskState.status = result.status === "finished" ? "FINISHED" : "ERROR";
  if (result.status !== "finished") {
    taskState.errorMessage = `Run ended with status ${result.status}`;
    return;
  }

  const parsed = extractMarkedJson(result.resultText);
  if (parsed.value !== undefined) {
    taskState.parsedResult = parsed.value;
  } else {
    taskState.parseError = parsed.error;
  }
}

function stitchPrompt(task: DagTask, stateById: Map<string, TaskState>): string {
  const upstream = task.depends_on
    .map((depId) => {
      const dep = stateById.get(depId);
      if (!dep) return "";
      const body = dep.resultText
        ? truncate(dep.resultText, UPSTREAM_SNIPPET_CAP)
        : dep.errorMessage
          ? `(failed: ${dep.errorMessage})`
          : "(no output)";
      return [`### ${depId} [${dep.status}]`, body].join("\n");
    })
    .filter(Boolean);

  if (upstream.length === 0) return task.subtask_prompt;
  return [
    "Upstream task results are provided for context. Use them; do not redo completed work.",
    "",
    ...upstream,
    "",
    "---",
    "",
    task.subtask_prompt,
  ].join("\n");
}

async function mapWithConcurrency<T>(
  items: T[],
  concurrency: number,
  fn: (item: T) => Promise<void>,
): Promise<void> {
  const limit = Math.max(1, concurrency);
  let index = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (index < items.length) {
      const item = items[index++];
      await fn(item);
    }
  });
  await Promise.all(workers);
}

async function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  onTimeout: () => void,
): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<T>((_, reject) => {
        timer = setTimeout(() => {
          onTimeout();
          reject(new Error(`Task timed out after ${timeoutMs}ms.`));
        }, timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

function truncate(value: string, max: number): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}...`;
}
