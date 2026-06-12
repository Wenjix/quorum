#!/usr/bin/env node
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import {
  canvasFileNameFor,
  canvasPathFromOutDir,
  cursorCanvasMirrorPath,
  writeCanvas,
} from "./canvas.js";
import { parseClusterIds, parseScoredClusters, selectClusters } from "./clusters.js";
import { CursorCloudAdapter } from "./cursor-cloud-adapter.js";
import { parseDag } from "./dag.js";
import { upsertExplorationComment } from "./github.js";
import { parsePullRequestRef, repoSlug } from "./pr.js";
import { buildExplorationDag } from "./prompts.js";
import { buildExplorationReport, renderExplorationMarkdown } from "./report.js";
import { initialRunState, runDag, writeState } from "./runner.js";
import { recoverScoredClustersFromPullRequest } from "./synthesis.js";
import type { Dag, ExplorationContext, RunnerContext, ScoredClustersDoc } from "./types.js";

interface ParsedArgs {
  command?: string;
  positionals: string[];
  flags: Map<string, string[]>;
}

interface ExploreExecutionOptions {
  repo: string;
  pr: string;
  scoredDoc: ScoredClustersDoc;
  parsed: ParsedArgs;
  planOnly: boolean;
  post: boolean;
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
  if (parsed.command === "plan-pr") {
    await prShortcutCommand(parsed, { planOnly: true, post: false });
    return;
  }
  if (parsed.command === "run-pr") {
    await prShortcutCommand(parsed, { planOnly: false, post: hasFlag(parsed, "post") });
    return;
  }
  if (parsed.command === "post-pr") {
    await prShortcutCommand(parsed, { planOnly: false, post: true });
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
  if (parsed.command === "canvas") {
    await canvasCommand(parsed);
    return;
  }
  throw new Error(`Unknown command: ${parsed.command}`);
}

async function explore(parsed: ParsedArgs): Promise<void> {
  const repo = requiredFlag(parsed, "repo");
  const pr = requiredFlag(parsed, "pr");
  const scoredPath = requiredFlag(parsed, "scored");
  const scoredDoc = parseScoredClusters(await readJson(scoredPath));
  const planOnly = hasFlag(parsed, "plan-only");
  const post = !(hasFlag(parsed, "no-post") || hasFlag(parsed, "dry-run") || planOnly);
  await executeExplore({ repo, pr, scoredDoc, parsed, planOnly, post });
}

async function prShortcutCommand(
  parsed: ParsedArgs,
  mode: { planOnly: boolean; post: boolean },
): Promise<void> {
  const ref = parsePullRequestRef(requiredPositional(parsed, "PR_URL"));
  const scoredDoc = await scoredDocFor(parsed, ref.repo, ref.pr);
  await executeExplore({
    repo: ref.repo,
    pr: ref.pr,
    scoredDoc,
    parsed,
    planOnly: mode.planOnly,
    post: mode.post,
  });
}

async function executeExplore(options: ExploreExecutionOptions): Promise<void> {
  const { repo, pr, scoredDoc, parsed, planOnly, post } = options;
  const selected = selectClusters(scoredDoc, {
    clusterIds: parseClusterIds(flagValues(parsed, "cluster")),
    minQuorum: numberFlag(parsed, "min-quorum", 2),
    maxClusters: optionalNumberFlag(parsed, "max-clusters"),
  });
  if (selected.length === 0) {
    throw new Error("No clusters selected. Lower --min-quorum or pass --cluster.");
  }

  const runId = createRunId(repo, pr);
  const outDir = flag(parsed, "out") ?? join(".quorum", "runs", runId);
  const repoUrl = flag(parsed, "repo-url") ?? `https://github.com/${repo}`;
  const prUrl = flag(parsed, "pr-url") ?? `https://github.com/${repo}/pull/${pr}`;
  const dag = buildExplorationDag({ repo, pr, selectedClusters: selected, scoredDoc });

  await mkdir(outDir, { recursive: true });
  await writeJson(join(outDir, "input.clusters.scored.json"), scoredDoc);
  await writeJson(join(outDir, "dag.json"), dag);

  let state = initialRunState(dag);
  const canvasPath = canvasPathFor(parsed, outDir);
  const canvasMirrorPath = canvasMirrorPathFor(parsed, canvasPath, repo, pr);
  if (hasFlag(parsed, "plan-only")) {
    await writeState(outDir, state, canvasPath, canvasMirrorPath);
  } else {
    state = await runDag(
      dag,
      runnerContext(parsed, repo, pr, repoUrl, prUrl, outDir, canvasPath, canvasMirrorPath),
      new CursorCloudAdapter(),
    );
  }

  const context = explorationContext(repo, pr, repoUrl, prUrl, runId);
  await writeReportArtifacts(outDir, context, scoredDoc, selected, state);
  logCanvasPaths(canvasPath, canvasMirrorPath);

  if (post) {
    const markdown = await readFile(join(outDir, "exploration.md"), "utf8");
    const result = await upsertExplorationComment(repo, pr, markdown);
    console.log(`PR comment ${result.action}${result.commentId ? `: ${result.commentId}` : ""}`);
  }

  console.log(`wrote ${outDir}`);
}

async function scoredDocFor(
  parsed: ParsedArgs,
  repo: string,
  pr: string,
): Promise<ScoredClustersDoc> {
  const scoredPath = flag(parsed, "scored");
  if (scoredPath) {
    return parseScoredClusters(await readJson(scoredPath));
  }

  console.log(`recovering Quorum synthesis from ${repo}#${pr}`);
  return await recoverScoredClustersFromPullRequest(repo, pr);
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
  const canvasMirrorPath = canvasMirrorPathFor(parsed, canvasPath, repo, pr);

  const state = hasFlag(parsed, "plan-only")
    ? initialRunState(dag)
    : await runDag(
        dag,
        runnerContext(parsed, repo, pr, repoUrl, prUrl, outDir, canvasPath, canvasMirrorPath),
        new CursorCloudAdapter(),
      );
  if (hasFlag(parsed, "plan-only")) await writeState(outDir, state, canvasPath, canvasMirrorPath);
  logCanvasPaths(canvasPath, canvasMirrorPath);
  console.log(`wrote ${outDir}`);
}

async function renderCanvasCommand(parsed: ParsedArgs): Promise<void> {
  const statePath = requiredFlag(parsed, "state");
  await renderCanvasFromStatePath(parsed, statePath);
}

async function canvasCommand(parsed: ParsedArgs): Promise<void> {
  const target = requiredPositional(parsed, "RUN_DIR_OR_STATE_JSON");
  const statePath = target.endsWith(".json") ? target : join(target, "state.json");
  await renderCanvasFromStatePath(parsed, statePath);
}

async function renderCanvasFromStatePath(parsed: ParsedArgs, statePath: string): Promise<void> {
  const state = (await readJson(statePath)) as ReturnType<typeof initialRunState>;
  const outPath = flag(parsed, "canvas-path") ?? join(dirname(statePath), "quorum-exploration.canvas.tsx");
  await writeCanvas(outPath, state);
  const mirrorPath = hasFlag(parsed, "no-canvas-mirror")
    ? undefined
    : cursorCanvasMirrorPath(basename(outPath));
  if (mirrorPath) await writeCanvas(mirrorPath, state);
  logCanvasPaths(outPath, mirrorPath);
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
  canvasMirrorPath: string | undefined,
): RunnerContext {
  return {
    repo,
    pr,
    repoUrl,
    prUrl,
    outDir,
    canvasPath,
    canvasMirrorPath,
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

function canvasMirrorPathFor(
  parsed: ParsedArgs,
  canvasPath: string | false,
  repo: string,
  pr: string,
): string | undefined {
  if (canvasPath === false || hasFlag(parsed, "no-canvas-mirror")) return undefined;
  return cursorCanvasMirrorPath(canvasFileNameFor(repo, pr));
}

function logCanvasPaths(canvasPath: string | false, canvasMirrorPath?: string): void {
  if (canvasPath) console.log(`canvas ${canvasPath}`);
  if (canvasMirrorPath) console.log(`canvas (Cursor) ${canvasMirrorPath}`);
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
  const positionals: string[] = [];
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (!arg.startsWith("--")) {
      positionals.push(arg);
      continue;
    }
    const key = arg.slice(2);
    const next = rest[i + 1];
    const value = next && !next.startsWith("--") ? next : "true";
    if (value !== "true") i++;
    flags.set(key, [...(flags.get(key) ?? []), value]);
  }
  return { command, positionals, flags };
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

function requiredPositional(parsed: ParsedArgs, label: string): string {
  const value = parsed.positionals[0];
  if (!value) throw new Error(`Missing ${label}.`);
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

function createRunId(repo: string, pr: string): string {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  return `${repoSlug(repo)}-pr${pr}-${ts}`;
}

function printHelp(): void {
  console.log(`Usage:
  quorum plan-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum run-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum post-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum canvas .quorum/runs/run-id
  quorum explore --repo OWNER/REPO --pr N --scored clusters.scored.json [options]
  quorum run-dag --dag dag.json --out .quorum/runs/run-id --repo OWNER/REPO [options]
  quorum render-canvas --state .quorum/runs/run-id/state.json [--canvas-path PATH]

Options:
  --cluster ID[,ID]        Explore explicit cluster IDs instead of quorum filter.
  --min-quorum N           Default: 2.
  --max-clusters N         Limit selected clusters.
  --scored PATH            Use a local clusters.scored.json instead of recovering it from the PR.
  --out DIR                Output directory. Default: .quorum/runs/<repo>-pr<N>-<timestamp>.
  --post                   For run-pr only: upsert the PR exploration comment after the run.
  --repo-url URL           Default: https://github.com/OWNER/REPO.
  --pr-url URL             Default: https://github.com/OWNER/REPO/pull/N.
  --concurrency N          Default: 4.
  --task-timeout-ms N      Default: 1200000.
  --dry-run | --no-post    Run cloud exploration but do not write a PR comment.
  --plan-only              Generate DAG/state/report shell without Cursor Cloud or GitHub calls.
  --no-stream              Do not consume run.stream(); wait for final result only.
  --canvas-path PATH       Write a Cursor Canvas artifact to this path.
  --no-canvas              Do not write the .canvas.tsx artifact.
  --no-canvas-mirror       Do not mirror the canvas into ~/.cursor/projects/<workspace>/canvases/.
`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
