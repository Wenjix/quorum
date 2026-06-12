#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { canvasPathFromOutDir, writeCanvas } from "./canvas.js";
import { parseClusterIds, parseScoredClusters, selectClusters } from "./clusters.js";
import { CursorCloudAdapter } from "./cursor-cloud-adapter.js";
import { parseDag } from "./dag.js";
import { upsertExplorationComment } from "./github.js";
import { buildExplorationDag } from "./prompts.js";
import { buildExplorationReport, renderExplorationMarkdown } from "./report.js";
import { initialRunState, runDag, writeState } from "./runner.js";
import type { Dag, ExplorationContext, RunnerContext, ScoredClustersDoc } from "./types.js";

interface ParsedArgs {
  command?: string;
  flags: Map<string, string[]>;
}

async function main(): Promise<void> {
  const parsed = parseArgs(process.argv.slice(2));
  if (!parsed.command || hasFlag(parsed, "help")) {
    printHelp();
    return;
  }

  if (parsed.command === "explore") {
    await explore(parsed);
    return;
  }
  if (parsed.command === "run-dag") {
    await runDagCommand(parsed);
    return;
  }
  if (parsed.command === "render-canvas") {
    await renderCanvasCommand(parsed);
    return;
  }
  throw new Error(`Unknown command: ${parsed.command}`);
}

async function explore(parsed: ParsedArgs): Promise<void> {
  const repo = requiredFlag(parsed, "repo");
  const pr = requiredFlag(parsed, "pr");
  const scoredPath = requiredFlag(parsed, "scored");
  const scoredDoc = parseScoredClusters(await readJson(scoredPath));
  const selected = selectClusters(scoredDoc, {
    clusterIds: parseClusterIds(flagValues(parsed, "cluster")),
    minQuorum: numberFlag(parsed, "min-quorum", 2),
    maxClusters: optionalNumberFlag(parsed, "max-clusters"),
  });
  if (selected.length === 0) {
    throw new Error("No clusters selected. Lower --min-quorum or pass --cluster.");
  }

  const runId = createRunId(pr);
  const outDir = flag(parsed, "out") ?? join(".quorum", "runs", runId);
  const repoUrl = flag(parsed, "repo-url") ?? `https://github.com/${repo}`;
  const prUrl = flag(parsed, "pr-url") ?? `https://github.com/${repo}/pull/${pr}`;
  const dag = buildExplorationDag({ repo, pr, selectedClusters: selected, scoredDoc });

  await mkdir(outDir, { recursive: true });
  await writeJson(join(outDir, "input.clusters.scored.json"), scoredDoc);
  await writeJson(join(outDir, "dag.json"), dag);

  let state = initialRunState(dag);
  const canvasPath = canvasPathFor(parsed, outDir);
  if (hasFlag(parsed, "plan-only")) {
    await writeState(outDir, state, canvasPath);
  } else {
    state = await runDag(
      dag,
      runnerContext(parsed, repo, pr, repoUrl, prUrl, outDir, canvasPath),
      new CursorCloudAdapter(),
    );
  }

  const context = explorationContext(repo, pr, repoUrl, prUrl, runId);
  await writeReportArtifacts(outDir, context, scoredDoc, selected, state);
  if (canvasPath) console.log(`canvas ${canvasPath}`);

  const noPost = hasFlag(parsed, "no-post") || hasFlag(parsed, "dry-run") || hasFlag(parsed, "plan-only");
  if (!noPost) {
    const markdown = await readFile(join(outDir, "exploration.md"), "utf8");
    const result = await upsertExplorationComment(repo, pr, markdown);
    console.log(`PR comment ${result.action}${result.commentId ? `: ${result.commentId}` : ""}`);
  }

  console.log(`wrote ${outDir}`);
}

async function runDagCommand(parsed: ParsedArgs): Promise<void> {
  const dagPath = requiredFlag(parsed, "dag");
  const outDir = requiredFlag(parsed, "out");
  const repo = flag(parsed, "repo") ?? "unknown/unknown";
  const pr = flag(parsed, "pr") ?? "0";
  const repoUrl = flag(parsed, "repo-url") ?? (repo === "unknown/unknown" ? undefined : `https://github.com/${repo}`);
  if (!repoUrl) throw new Error("run-dag requires --repo-url or --repo.");
  const prUrl = flag(parsed, "pr-url") ?? (repo !== "unknown/unknown" && pr !== "0" ? `https://github.com/${repo}/pull/${pr}` : undefined);
  const dag = parseDag(await readJson(dagPath));
  await mkdir(outDir, { recursive: true });
  await writeJson(join(outDir, "dag.json"), dag);
  const canvasPath = canvasPathFor(parsed, outDir);

  const state = hasFlag(parsed, "plan-only")
    ? initialRunState(dag)
    : await runDag(
        dag,
        runnerContext(parsed, repo, pr, repoUrl, prUrl, outDir, canvasPath),
        new CursorCloudAdapter(),
      );
  if (hasFlag(parsed, "plan-only")) await writeState(outDir, state, canvasPath);
  if (canvasPath) console.log(`canvas ${canvasPath}`);
  console.log(`wrote ${outDir}`);
}

async function renderCanvasCommand(parsed: ParsedArgs): Promise<void> {
  const statePath = requiredFlag(parsed, "state");
  const state = (await readJson(statePath)) as ReturnType<typeof initialRunState>;
  const outPath = flag(parsed, "canvas-path") ?? join(dirname(statePath), "quorum-exploration.canvas.tsx");
  await writeCanvas(outPath, state);
  console.log(`canvas ${outPath}`);
}

async function writeReportArtifacts(
  outDir: string,
  context: ExplorationContext,
  scoredDoc: ScoredClustersDoc,
  selected: ScoredClustersDoc["clusters"],
  state: Awaited<ReturnType<typeof runDag>> | ReturnType<typeof initialRunState>,
): Promise<void> {
  const report = buildExplorationReport(context, scoredDoc, selected, state);
  const markdown = renderExplorationMarkdown(report);
  await writeJson(join(outDir, "exploration.json"), report);
  await writeFile(join(outDir, "exploration.md"), markdown, "utf8");
}

function runnerContext(
  parsed: ParsedArgs,
  repo: string,
  pr: string,
  repoUrl: string,
  prUrl: string | undefined,
  outDir: string,
  canvasPath: string | false,
): RunnerContext {
  return {
    repo,
    pr,
    repoUrl,
    prUrl,
    outDir,
    canvasPath,
    apiKey: flag(parsed, "api-key") ?? process.env.CURSOR_API_KEY,
    concurrency: numberFlag(parsed, "concurrency", 4),
    taskTimeoutMs: numberFlag(parsed, "task-timeout-ms", 20 * 60 * 1000),
    stream: !hasFlag(parsed, "no-stream"),
  };
}

function canvasPathFor(parsed: ParsedArgs, outDir: string): string | false {
  if (hasFlag(parsed, "no-canvas")) return false;
  return flag(parsed, "canvas-path") ?? canvasPathFromOutDir(outDir);
}

function explorationContext(
  repo: string,
  pr: string,
  repoUrl: string,
  prUrl: string | undefined,
  runId: string,
): ExplorationContext {
  return {
    repo,
    pr,
    repoUrl,
    prUrl,
    runId,
    generatedAt: new Date().toISOString(),
  };
}

function parseArgs(argv: string[]): ParsedArgs {
  let command: string | undefined = argv[0];
  let rest = argv.slice(1);
  if (!command || command.startsWith("--")) {
    command = undefined;
    rest = argv;
  }
  const flags = new Map<string, string[]>();
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (!arg.startsWith("--")) {
      throw new Error(`Unexpected positional argument: ${arg}`);
    }
    const key = arg.slice(2);
    const next = rest[i + 1];
    const value = next && !next.startsWith("--") ? next : "true";
    if (value !== "true") i++;
    flags.set(key, [...(flags.get(key) ?? []), value]);
  }
  return { command, flags };
}

function flag(parsed: ParsedArgs, name: string): string | undefined {
  return parsed.flags.get(name)?.at(-1);
}

function flagValues(parsed: ParsedArgs, name: string): string[] {
  return parsed.flags.get(name) ?? [];
}

function hasFlag(parsed: ParsedArgs, name: string): boolean {
  return parsed.flags.has(name);
}

function requiredFlag(parsed: ParsedArgs, name: string): string {
  const value = flag(parsed, name);
  if (!value || value === "true") throw new Error(`Missing --${name}.`);
  return value;
}

function numberFlag(parsed: ParsedArgs, name: string, fallback: number): number {
  return optionalNumberFlag(parsed, name) ?? fallback;
}

function optionalNumberFlag(parsed: ParsedArgs, name: string): number | undefined {
  const value = flag(parsed, name);
  if (value === undefined) return undefined;
  const parsedValue = Number(value);
  if (!Number.isSafeInteger(parsedValue) || parsedValue <= 0) {
    throw new Error(`--${name} must be a positive integer.`);
  }
  return parsedValue;
}

async function readJson(path: string): Promise<unknown> {
  return JSON.parse(await readFile(path, "utf8"));
}

async function writeJson(path: string, value: unknown): Promise<void> {
  await writeFile(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function createRunId(pr: string): string {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  return `${ts}-pr${pr}`;
}

function printHelp(): void {
  console.log(`Usage:
  quorum-cloud explore --repo OWNER/REPO --pr N --scored clusters.scored.json [options]
  quorum-cloud run-dag --dag dag.json --out .quorum/runs/run-id --repo OWNER/REPO [options]
  quorum-cloud render-canvas --state .quorum/runs/run-id/state.json [--canvas-path PATH]

Options:
  --cluster ID[,ID]        Explore explicit cluster IDs instead of quorum filter.
  --min-quorum N           Default: 2.
  --max-clusters N         Limit selected clusters.
  --out DIR                Output directory. Default: .quorum/runs/<timestamp>-pr<N>.
  --repo-url URL           Default: https://github.com/OWNER/REPO.
  --pr-url URL             Default: https://github.com/OWNER/REPO/pull/N.
  --concurrency N          Default: 4.
  --task-timeout-ms N      Default: 1200000.
  --dry-run | --no-post    Run cloud exploration but do not write a PR comment.
  --plan-only              Generate DAG/state/report shell without Cursor Cloud or GitHub calls.
  --no-stream              Do not consume run.stream(); wait for final result only.
  --canvas-path PATH       Write a Cursor Canvas artifact to this path.
  --no-canvas              Do not write the .canvas.tsx artifact.
`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
