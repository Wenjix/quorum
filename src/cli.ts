#!/usr/bin/env node
import { execFile } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { promisify } from "node:util";
import { AnthropicAdapter } from "./adapters/anthropic.js";
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
import { loadRunLogs } from "./logging.js";
import type { LogEntry } from "./logging.js";
import { scriptsDirFromModuleUrl } from "./paths.js";
import { parsePullRequestRef, repoSlug } from "./pr.js";
import { buildExplorationDag } from "./prompts.js";
import { buildExplorationReport, renderExplorationMarkdown } from "./report.js";
import { initialRunState, runDag, writeState } from "./runner.js";
import { recoverScoredClustersFromPullRequest } from "./synthesis.js";
import type {
  Dag,
  ExplorationContext,
  RunnerContext,
  ScoredClustersDoc,
  TaskRunnerAdapter,
} from "./types.js";

const execFileAsync = promisify(execFile);

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
  if (parsed.command === "synthesize") {
    await synthesizeCommand(parsed);
    return;
  }
  if (parsed.command === "triage-pr") {
    await triagePrCommand(parsed);
    return;
  }
  if (parsed.command === "setup") {
    await setupCommand();
    return;
  }
  if (parsed.command === "eval") {
    await evalCommand(parsed);
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

  let state = initialRunState(dag, resolveProvider(parsed));
  const canvasPath = canvasPathFor(parsed, outDir);
  const canvasMirrorPath = canvasMirrorPathFor(parsed, canvasPath, repo, pr);
  if (hasFlag(parsed, "plan-only")) {
    await writeState(outDir, state, canvasPath, canvasMirrorPath);
  } else {
    state = await runDag(
      dag,
      runnerContext(parsed, repo, pr, repoUrl, prUrl, outDir, canvasPath, canvasMirrorPath),
      resolveAdapter(parsed),
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
    ? initialRunState(dag, resolveProvider(parsed))
    : await runDag(
        dag,
        runnerContext(parsed, repo, pr, repoUrl, prUrl, outDir, canvasPath, canvasMirrorPath),
        resolveAdapter(parsed),
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
  const provider = resolveProvider(parsed);
  const apiKey = flag(parsed, "api-key")
    ?? (provider === "anthropic" ? process.env.ANTHROPIC_API_KEY : process.env.CURSOR_API_KEY);
  return {
    repo,
    pr,
    repoUrl,
    prUrl,
    outDir,
    canvasPath,
    canvasMirrorPath,
    apiKey,
    // Default to 4 simultaneous tasks for both providers. Cursor Cloud plans may
    // cap concurrent agents; if that cap is hit, the adapter surfaces an
    // actionable error (see describeCursorError) telling the user to lower
    // --concurrency or upgrade their plan.
    concurrency: numberFlag(parsed, "concurrency", 4),
    taskTimeoutMs: numberFlag(parsed, "task-timeout-ms", 20 * 60 * 1000),
    stream: !hasFlag(parsed, "no-stream") && provider === "cursor",
    provider,
  };
}

function resolveProvider(parsed: ParsedArgs): "cursor" | "anthropic" {
  const provider = flag(parsed, "provider") ?? process.env.QUORUM_PROVIDER ?? "cursor";
  return provider === "anthropic" ? "anthropic" : "cursor";
}

function resolveAdapter(parsed: ParsedArgs): TaskRunnerAdapter {
  return resolveProvider(parsed) === "anthropic"
    ? new AnthropicAdapter()
    : new CursorCloudAdapter();
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

// ---- synthesize command ----

async function synthesizeCommand(parsed: ParsedArgs): Promise<void> {
  const ref = parsePullRequestRef(requiredPositional(parsed, "OWNER/REPO#PR or PR_URL"));
  const outDir = flag(parsed, "out") ?? join(".quorum", "synthesis", `${repoSlug(ref.repo)}-pr${ref.pr}`);

  const findingsPath = flag(parsed, "findings") ?? join(outDir, "findings.json");
  const clustersPath = flag(parsed, "clusters") ?? join(outDir, "clusters.json");
  const scoredPath = flag(parsed, "out-file") ?? join(outDir, "clusters.scored.json");

  await mkdir(outDir, { recursive: true });

  // Step 1: Fetch findings
  console.log(`Fetching findings for ${ref.repo}#${ref.pr}...`);
  try {
    await execFileAsync("bash", [
      join(repoScriptsDir(), "fetch_findings.sh"),
      ref.repo,
      ref.pr,
      findingsPath,
    ]);
  } catch {
    throw new Error("Failed to fetch findings. Ensure gh CLI is authenticated and jq is installed.");
  }

  const findings = await readJson(findingsPath);
  if (!Array.isArray(findings) || findings.length === 0) {
    throw new Error("No bot findings found. Run the Quorum skill for clustering, or check QUORUM_BOTS.");
  }

  // If user provided clusters.json, use it; otherwise generate all-singletons
  let clustersExists = false;
  try {
    await readFile(clustersPath);
    clustersExists = true;
  } catch { /* file doesn't exist */ }

  if (!clustersExists) {
    console.log("No clusters.json provided; generating all-singletons clustering.");
    await generateSingletonClusters(findings, clustersPath);
  }

  // Step 3: Validate & score
  console.log("Validating and scoring...");
  try {
    await execFileAsync("python3", [
      join(repoScriptsDir(), "validate_partition.py"),
      findingsPath,
      clustersPath,
      "-o",
      scoredPath,
    ]);
  } catch (error) {
    const err = error as { stderr?: string; message?: string };
    console.error(err.stderr || err.message || String(error));
    throw new Error("Validation failed. Fix clusters.json or re-run with all-singletons.");
  }

  console.log(`wrote ${scoredPath}`);

  // Step 4: Post (or dry-run)
  const dryRun = hasFlag(parsed, "dry-run");
  const noPost = hasFlag(parsed, "no-post");
  if (!noPost) {
    const args = ["python3", join(repoScriptsDir(), "post_synthesis.py"), ref.repo, ref.pr, scoredPath];
    if (dryRun) args.push("--dry-run");
    if (hasFlag(parsed, "minimize")) args.push("--minimize");
    if (hasFlag(parsed, "no-reactions")) args.push("--no-reactions");

    try {
      const { stdout, stderr } = await execFileAsync("python3", args.slice(1));
      if (dryRun) {
        console.log(stdout);
        if (stderr) console.error(stderr);
      } else {
        console.log(stdout.trim());
      }
    } catch (error) {
      const err = error as { stderr?: string; message?: string };
      console.error(err.stderr || err.message || String(error));
      throw new Error("post_synthesis.py failed.");
    }
  }
}

async function generateSingletonClusters(findings: unknown[], outPath: string): Promise<void> {
  const clusters = (findings as Array<Record<string, unknown>>).map((finding: Record<string, unknown>) => {
    const lines = (finding.lines as [number | null, number | null]) ?? [null, null];
    return {
      cluster_id: `${finding.id}-solo`,
      member_ids: [finding.id],
      canonical_title: firstLine(String(finding.body ?? "No description")),
      canonical_description: firstLine(String(finding.body ?? "No description"), 240),
      category: "other",
      severity: "minor",
      primary_location: {
        file: finding.file,
        start_line: lines[0],
        end_line: lines[1],
      },
      match_type: "singleton",
      match_confidence: 1.0,
      cross_file: false,
    };
  });
  await writeJson(outPath, { clusters });
}

function firstLine(text: string, limit = 80): string {
  for (const line of (text || "").trim().split("\n")) {
    const cleaned = line.trim().replace(/^[#*>\-\s]+/, "").trim();
    if (cleaned) return cleaned.slice(0, limit);
  }
  return "(no comment text)";
}

// ---- triage-pr command ----

async function triagePrCommand(parsed: ParsedArgs): Promise<void> {
  const ref = parsePullRequestRef(requiredPositional(parsed, "PR_URL"));
  const tmpDir = join(".quorum", "triage", `${repoSlug(ref.repo)}-pr${ref.pr}`);
  const scoredPath = join(tmpDir, "clusters.scored.json");

  // Step 1: Synthesize
  console.log("=== Phase 1: Synthesis ===");
  const synthOverrides: Record<string, string> = {
    out: flag(parsed, "synth-out") ?? tmpDir,
    "out-file": scoredPath,
  };
  // When plan-only, suppress synthesis posting too since triage-pr is read-only
  if (hasFlag(parsed, "plan-only")) {
    synthOverrides["no-post"] = "true";
  }
  const synthParsed = cloneParsedWith(parsed, synthOverrides);
  await synthesizeCommand(synthParsed);

  // Step 2: Explore
  console.log("\n=== Phase 2: Exploration ===");
  const runId = createRunId(ref.repo, ref.pr);
  const outDir = flag(parsed, "out") ?? join(".quorum", "runs", runId);
  const scoredDoc = parseScoredClusters(await readJson(scoredPath));
  await executeExplore({
    repo: ref.repo,
    pr: ref.pr,
    scoredDoc,
    parsed,
    planOnly: hasFlag(parsed, "plan-only"),
    post: !hasFlag(parsed, "no-post") && !hasFlag(parsed, "dry-run"),
  });
}

function cloneParsedWith(parsed: ParsedArgs, overrides: Record<string, string>): ParsedArgs {
  const cloned = new Map(parsed.flags);
  for (const [key, value] of Object.entries(overrides)) {
    cloned.set(key, [value]);
  }
  return { command: parsed.command, positionals: [...parsed.positionals], flags: cloned };
}

// ---- setup command ----

async function setupCommand(): Promise<void> {
  const checks: Array<{ name: string; ok: boolean; help: string }> = [];

  // Node
  const nodeVersion = process.versions.node;
  const nodeOk = parseInt(nodeVersion.split(".")[0], 10) >= 22;
  checks.push({
    name: `Node.js >= 22 (found ${nodeVersion})`,
    ok: nodeOk,
    help: "Install Node.js 22+ from https://nodejs.org",
  });

  // gh CLI
  try {
    await execFileAsync("gh", ["--version"]);
    checks.push({ name: "gh CLI", ok: true, help: "" });
  } catch {
    checks.push({ name: "gh CLI", ok: false, help: "Install from https://cli.github.com" });
  }

  // jq
  try {
    await execFileAsync("jq", ["--version"]);
    checks.push({ name: "jq", ok: true, help: "" });
  } catch {
    checks.push({ name: "jq", ok: false, help: "Install with brew install jq or apt install jq" });
  }

  // python3
  try {
    await execFileAsync("python3", ["--version"]);
    checks.push({ name: "python3", ok: true, help: "" });
  } catch {
    checks.push({ name: "python3", ok: false, help: "Install from https://python.org" });
  }

  // API keys
  const cursorKey = !!process.env.CURSOR_API_KEY;
  const anthropicKey = !!process.env.ANTHROPIC_API_KEY;
  const githubToken = !!process.env.GITHUB_TOKEN || !!process.env.GH_TOKEN;
  checks.push({
    name: `CURSOR_API_KEY (${cursorKey ? "set" : "not set"})`,
    ok: cursorKey || anthropicKey,
    help: "Set CURSOR_API_KEY for Cursor Cloud or ANTHROPIC_API_KEY for Anthropic",
  });
  checks.push({
    name: `GITHUB_TOKEN (${githubToken ? "set" : "not set"}, optional)`,
    ok: true,
    help: "Set GITHUB_TOKEN for direct GitHub API access (falls back to gh CLI)",
  });

  // Skill installation
  const home = process.env.HOME || process.env.USERPROFILE || "~";
  const cursorSkillDir = ".cursor/skills/quorum";
  const claudeSkillDir = ".claude/skills/quorum";
  const hasCursorSkill = await dirExists(join(home, cursorSkillDir));
  const hasClaudeSkill = await dirExists(join(home, claudeSkillDir));
  checks.push({
    name: `Cursor skill (${hasCursorSkill ? "installed" : "not installed"})`,
    ok: hasCursorSkill || hasClaudeSkill,
    help: `Run: mkdir -p ${cursorSkillDir} && unzip quorum.skill -d ${cursorSkillDir}`,
  });

  console.log("Quorum Setup Check\n");
  let allOk = true;
  for (const check of checks) {
    const icon = check.ok ? "PASS" : "FAIL";
    console.log(`  [${icon}] ${check.name}`);
    if (!check.ok) {
      allOk = false;
      console.log(`        -> ${check.help}`);
    }
  }

  if (allOk) {
    console.log("\nAll checks passed. Quorum is ready to use.");
  } else {
    console.log("\nFix the FAIL items above, then re-run 'quorum setup'.");
    process.exit(1);
  }
}

async function dirExists(path: string): Promise<boolean> {
  try {
    const stat = await import("node:fs/promises").then((fs) => fs.stat(path));
    return stat.isDirectory();
  } catch {
    return false;
  }
}

// ---- eval command ----

async function evalCommand(parsed: ParsedArgs): Promise<void> {
  // Leave logDir undefined when --log-dir is absent so loadRunLogs recursively
  // scans .quorum/runs/ (where per-run logs live) instead of a flat directory.
  const logDir = flag(parsed, "log-dir");
  const entries = await loadRunLogs(logDir);

  if (entries.length === 0) {
    const scanned = logDir ?? join(".quorum", "runs");
    console.log("No run logs found in", scanned);
    console.log("Run some explorations first to populate the log.");
    return;
  }

  const runs = new Map<string, LogEntry[]>();
  for (const entry of entries) {
    const key = entry.runTitle ?? "unknown";
    if (!runs.has(key)) runs.set(key, []);
    runs.get(key)!.push(entry);
  }

  console.log(`Eval Report — ${runs.size} run(s), ${entries.length} event(s)\n`);

  // Task success rate — a finished task with parseError counts as degraded
  const tasks = entries.filter((e) => e.type === "task_end" || e.type === "task_error" || e.type === "task_skip");
  const finishedClean = tasks.filter(
    (e) => e.type === "task_end" && e.status === "FINISHED" && !e.parseError,
  ).length;
  const finishedDegraded = tasks.filter(
    (e) => e.type === "task_end" && e.status === "FINISHED" && e.parseError,
  ).length;
  const finished = finishedClean + finishedDegraded;
  const errors = tasks.filter((e) => e.type === "task_error" || e.status === "ERROR").length;
  const skipped = tasks.filter((e) => e.type === "task_skip").length;

  console.log("## Task Outcomes");
  console.log(`  Total tasks: ${tasks.length}`);
  console.log(`  Finished clean: ${finishedClean}`);
  if (finishedDegraded > 0) {
    console.log(`  Finished (degraded parse): ${finishedDegraded}`);
  }
  console.log(`  Finished total: ${finished} (${tasks.length ? ((finished / tasks.length) * 100).toFixed(0) : 0}%)`);
  console.log(`  Errors: ${errors}`);
  console.log(`  Skipped: ${skipped}`);
  console.log("");

  // Cluster-level stats
  const rootCauseTasks = entries.filter((e) => e.taskType === "root_cause");
  const sweepTasks = entries.filter((e) => e.taskType === "pattern_sweep");
  const clusterIds = new Set([...rootCauseTasks, ...sweepTasks].map((e) => e.clusterId).filter(Boolean));
  console.log(`## Clusters Explored: ${clusterIds.size}`);

  // Average duration
  const durations = entries
    .filter((e) => e.durationMs !== undefined && e.durationMs > 0)
    .map((e) => e.durationMs!);
  if (durations.length > 0) {
    const avg = durations.reduce((a, b) => a + b, 0) / durations.length;
    console.log(`  Average task duration: ${(avg / 1000).toFixed(1)}s`);
  }

  console.log("");
  console.log("## Runs");
  for (const [title, runEntries] of runs) {
    const dagStart = runEntries.find((e) => e.type === "dag_start");
    const dagEnd = runEntries.find((e) => e.type === "dag_end");
    const status = dagEnd?.status ?? "unknown";
    console.log(`  ${title}: ${status} (${runEntries.length} events)`);
  }
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

function repoScriptsDir(): string {
  // When installed as an npm package or running from source, scripts live in
  // <package>/scripts/. Resolution (incl. Windows + spaces) lives in paths.ts.
  return scriptsDirFromModuleUrl(import.meta.url);
}

function printHelp(): void {
  console.log(`Usage:
  quorum synthesize OWNER/REPO#PR [options]
  quorum triage-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum plan-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum run-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum post-pr https://github.com/OWNER/REPO/pull/N [options]
  quorum canvas .quorum/runs/run-id
  quorum setup
  quorum eval [--log-dir .quorum/log]
  quorum explore --repo OWNER/REPO --pr N --scored clusters.scored.json [options]
  quorum run-dag --dag dag.json --out .quorum/runs/run-id --repo OWNER/REPO [options]

Commands:
  synthesize          Run Phase 1 synthesis pipeline (fetch, score, post) without AI clustering.
  triage-pr           Run synthesis + exploration end-to-end on a PR.
  plan-pr             Generate DAG/Canvas without calling cloud agents.
  run-pr              Run Cursor Cloud or Anthropic exploration (no PR comment by default).
  post-pr             Run exploration and upsert the PR exploration comment.
  canvas              Regenerate or open a Canvas from a saved run directory.
  setup               Validate prerequisites (Node, gh, jq, python3, API keys, skills).
  eval                Compute reviewer precision and success stats from run logs.

Options:
  --provider cursor|anthropic  Backend for exploration agents. Default: cursor.
  --cluster ID[,ID]            Explore explicit cluster IDs instead of quorum filter.
  --min-quorum N               Default: 2.
  --max-clusters N             Limit selected clusters.
  --scored PATH                Use a local clusters.scored.json.
  --out DIR                    Output directory.
  --post                       For run-pr only: upsert the PR exploration comment.
  --concurrency N              Simultaneous tasks. Default: 4.
  --task-timeout-ms N          Default: 1200000.
  --dry-run | --no-post        Do not write a PR comment.
  --plan-only                  Generate DAG/state/report without cloud or GitHub calls.
  --no-stream                  Disable streaming (only supported for Cursor provider).
  --canvas-path PATH           Custom Canvas artifact path.
  --no-canvas                  Skip Canvas generation.
  --no-canvas-mirror           Skip mirroring into Cursor canvases dir.
  --api-key KEY                Override the default API key for the selected provider.

Environment:
  CURSOR_API_KEY               Cursor Cloud API key.
  ANTHROPIC_API_KEY            Anthropic API key (required for --provider anthropic).
  GITHUB_TOKEN                 GitHub API token (falls back to gh CLI).
  QUORUM_PROVIDER              Default: cursor. Set to anthropic for direct Anthropic API.
  QUORUM_MODEL_HIGH            Model for HIGH complexity tasks.
  QUORUM_MODEL_MED             Model for MED complexity tasks.
  QUORUM_MODEL_LOW             Model for LOW complexity tasks.
`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
