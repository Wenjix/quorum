import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import ts from "typescript";
import { parseDag } from "../src/dag.js";
import { runDag } from "../src/runner.js";
import { FakeAdapter, markedResult } from "./helpers.js";
import type { RunnerContext, TaskExecutionInput } from "../src/types.js";

test("runDag stitches upstream output and stores parsed task results", async () => {
  const dag = parseDag({
    title: "demo",
    tasks: [
      { id: "root", depends_on: [], complexity: "LOW", subtask_prompt: "root prompt" },
      { id: "child", depends_on: ["root"], complexity: "LOW", subtask_prompt: "child prompt" },
    ],
  });
  const outDir = await mkdtemp(join(tmpdir(), "quorum-runner-"));
  const adapter = new FakeAdapter({
    root: { status: "finished", resultText: markedResult("root_cause"), runId: "run-root" },
    child: { status: "finished", resultText: markedResult("pattern_sweep"), runId: "run-child" },
  });

  const state = await runDag(dag, context(outDir), adapter);

  assert.equal(state.runOutcome, "SUCCESS");
  assert.equal(state.tasks[0].status, "FINISHED");
  assert.equal(state.tasks[1].status, "FINISHED");
  assert.match(adapter.prompts.get("child") ?? "", /Upstream task results/);
  assert.match(adapter.prompts.get("child") ?? "", /root summary/);
  assert.ok(state.tasks[0].parsedResult);
});

test("runDag writes a Cursor Canvas artifact by default", async () => {
  const dag = parseDag({
    title: "canvas demo",
    tasks: [{ id: "root", depends_on: [], complexity: "LOW", subtask_prompt: "root prompt" }],
  });
  const outDir = await mkdtemp(join(tmpdir(), "quorum-runner-"));
  await runDag(
    dag,
    context(outDir),
    new FakeAdapter({
      root: { status: "finished", resultText: markedResult("root_cause"), runId: "run-root" },
    }),
  );

  const canvas = await readFile(join(outDir, "quorum-exploration.canvas.tsx"), "utf8");
  assert.match(canvas, /cursor\/canvas/);
  assert.match(canvas, /canvas demo/);
  assert.match(canvas, /root/);
  assert.match(canvas, /FINISHED/);
  const transpiled = ts.transpileModule(canvas, {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
    reportDiagnostics: true,
  });
  assert.deepEqual(transpiled.diagnostics ?? [], []);
});

test("runDag skips dependents when a parent fails", async () => {
  const dag = parseDag({
    title: "failure",
    tasks: [
      { id: "root", depends_on: [], complexity: "LOW", subtask_prompt: "root" },
      { id: "child", depends_on: ["root"], complexity: "LOW", subtask_prompt: "child" },
    ],
  });
  const outDir = await mkdtemp(join(tmpdir(), "quorum-runner-"));
  const state = await runDag(
    dag,
    context(outDir),
    new FakeAdapter({
      root: { status: "error", resultText: "failed" },
    }),
  );

  assert.equal(state.runOutcome, "FAILED");
  assert.equal(state.tasks[0].status, "ERROR");
  assert.equal(state.tasks[1].status, "SKIPPED");
});

test("runDag records parse warnings without failing the task", async () => {
  const dag = parseDag({
    title: "parse",
    tasks: [{ id: "root", depends_on: [], complexity: "LOW", subtask_prompt: "root" }],
  });
  const outDir = await mkdtemp(join(tmpdir(), "quorum-runner-"));
  const state = await runDag(
    dag,
    context(outDir),
    new FakeAdapter({
      root: { status: "finished", resultText: "plain text only" },
    }),
  );

  assert.equal(state.runOutcome, "SUCCESS");
  assert.equal(state.tasks[0].status, "FINISHED");
  assert.match(state.tasks[0].parseError ?? "", /No parseable JSON/);
});

test("runDag times out and aborts the task signal", async () => {
  const dag = parseDag({
    title: "timeout",
    tasks: [{ id: "root", depends_on: [], complexity: "LOW", subtask_prompt: "root" }],
  });
  const outDir = await mkdtemp(join(tmpdir(), "quorum-runner-"));
  let aborted = false;
  const state = await runDag(
    dag,
    context(outDir, { taskTimeoutMs: 10 }),
    new FakeAdapter({
      root: (input: TaskExecutionInput) =>
        new Promise((_, reject) => {
          input.signal.addEventListener("abort", () => {
            aborted = true;
            reject(new Error("aborted"));
          });
        }),
    }),
  );

  assert.equal(aborted, true);
  assert.equal(state.tasks[0].status, "ERROR");
  assert.match(state.tasks[0].errorMessage ?? "", /timed out|aborted/);
});

function context(outDir: string, overrides: Partial<RunnerContext> = {}): RunnerContext {
  return {
    repo: "owner/repo",
    pr: "1",
    repoUrl: "https://github.com/owner/repo",
    prUrl: "https://github.com/owner/repo/pull/1",
    outDir,
    apiKey: "test",
    concurrency: 2,
    taskTimeoutMs: 1_000,
    stream: false,
    ...overrides,
  };
}
