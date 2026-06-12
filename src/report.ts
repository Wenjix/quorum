import type {
  ClusterExploration,
  ExplorationContext,
  ExplorationReport,
  RunState,
  ScoredCluster,
  ScoredClustersDoc,
  TaskState,
} from "./types.js";

export function buildExplorationReport(
  context: ExplorationContext,
  scoredDoc: ScoredClustersDoc,
  selectedClusters: ScoredCluster[],
  state: RunState,
): ExplorationReport {
  const tasks = state.tasks.map((task) => ({
    id: task.id,
    cluster_id: task.cluster_id,
    task_type: task.task_type,
    status: task.status,
    agentId: task.agentId,
    runId: task.runId,
    errorMessage: task.errorMessage,
    parseError: task.parseError,
  }));

  return {
    context,
    source: {
      reviewer_denominator: scoredDoc.totals.reviewer_denominator,
      selected_clusters: selectedClusters.map((cluster) => cluster.cluster_id),
    },
    clusters: selectedClusters.map((cluster) => summarizeCluster(cluster, state.tasks)),
    tasks,
  };
}

export function renderExplorationMarkdown(report: ExplorationReport): string {
  const lines: string[] = [];
  lines.push("# Quorum Exploration");
  lines.push("");
  lines.push(
    `Explored ${report.clusters.length} cluster(s) from ${report.context.repo}#${report.context.pr}.`,
  );
  lines.push("");

  for (const item of report.clusters) {
    const cluster = item.cluster;
    lines.push(`## ${cluster.cluster_id}: ${cluster.canonical_title}`);
    lines.push("");
    lines.push(
      [
        `Quorum ${cluster.quorum}/${report.source.reviewer_denominator}`,
        `severity ${cluster.severity}`,
        `category ${cluster.category}`,
        locationText(cluster),
      ].join(" | "),
    );
    if (cluster.canonical_description) {
      lines.push("");
      lines.push(cluster.canonical_description);
    }
    if (item.warnings.length > 0) {
      lines.push("");
      lines.push("Warnings:");
      for (const warning of item.warnings) lines.push(`- ${warning}`);
    }

    lines.push("");
    lines.push("### Root Cause");
    lines.push("");
    lines.push(renderRootCause(item.root_cause, item.root_cause_raw));

    lines.push("");
    lines.push("### Pattern Sweep");
    lines.push("");
    lines.push(renderPatternSweep(item.pattern_sweep, item.pattern_sweep_raw));
    lines.push("");
  }

  lines.push("## Task Runs");
  lines.push("");
  for (const task of report.tasks) {
    const run = task.runId ? ` run ${task.runId}` : "";
    const agent = task.agentId ? ` agent ${task.agentId}` : "";
    lines.push(`- ${task.id}: ${task.status}${agent}${run}`);
  }

  lines.push("");
  lines.push(`<sub>Quorum exploration generated ${report.context.generatedAt}</sub>`);
  return lines.join("\n");
}

function summarizeCluster(cluster: ScoredCluster, tasks: TaskState[]): ClusterExploration {
  const root = taskFor(tasks, cluster.cluster_id, "root_cause");
  const sweep = taskFor(tasks, cluster.cluster_id, "pattern_sweep");
  const warnings: string[] = [];

  for (const task of [root, sweep]) {
    if (!task) continue;
    if (task.status !== "FINISHED") {
      warnings.push(`${task.id} ended with status ${task.status}: ${task.errorMessage ?? "no detail"}`);
    }
    if (task.parseError) warnings.push(`${task.id}: ${task.parseError}`);
  }

  return {
    cluster,
    root_cause: root?.parsedResult,
    pattern_sweep: sweep?.parsedResult,
    root_cause_raw: root?.parsedResult ? undefined : root?.resultText,
    pattern_sweep_raw: sweep?.parsedResult ? undefined : sweep?.resultText,
    warnings,
  };
}

function taskFor(tasks: TaskState[], clusterId: string, taskType: string): TaskState | undefined {
  return tasks.find((task) => task.cluster_id === clusterId && task.task_type === taskType);
}

function renderRootCause(value: unknown, raw?: string): string {
  const obj = asRecord(value);
  if (!obj) return raw ? fenced(raw) : "_No structured root-cause result._";

  const lines: string[] = [];
  pushField(lines, "Summary", obj.summary);
  pushField(lines, "Mechanism", obj.mechanism);
  pushField(lines, "Missing invariant", obj.missing_invariant);
  pushField(lines, "Confidence", obj.confidence);
  pushArray(lines, "Evidence", obj.evidence);
  pushArray(lines, "Follow-ups", obj.follow_up_questions);
  return lines.length > 0 ? lines.join("\n") : fenced(JSON.stringify(obj, null, 2));
}

function renderPatternSweep(value: unknown, raw?: string): string {
  const obj = asRecord(value);
  if (!obj) return raw ? fenced(raw) : "_No structured pattern-sweep result._";

  const lines: string[] = [];
  pushArray(lines, "Search strategy", obj.search_strategy);
  pushArray(lines, "Matches", obj.matches);
  pushArray(lines, "Recommended actions", obj.recommended_actions);
  return lines.length > 0 ? lines.join("\n") : fenced(JSON.stringify(obj, null, 2));
}

function pushField(lines: string[], label: string, value: unknown): void {
  if (typeof value === "string" && value.trim()) {
    lines.push(`**${label}:** ${value.trim()}`);
  }
}

function pushArray(lines: string[], label: string, value: unknown): void {
  if (!Array.isArray(value) || value.length === 0) return;
  lines.push(`**${label}:**`);
  for (const item of value) {
    lines.push(`- ${formatArrayItem(item)}`);
  }
}

function formatArrayItem(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object" || Array.isArray(value)) return JSON.stringify(value);
  const obj = value as Record<string, unknown>;
  const parts = Object.entries(obj)
    .filter(([, v]) => v !== undefined && v !== null && String(v).trim() !== "")
    .map(([k, v]) => `${k}: ${String(v)}`);
  return parts.join(" | ");
}

function locationText(cluster: ScoredCluster): string {
  const loc = cluster.primary_location;
  if (!loc?.file) return "location unknown";
  const start = loc.start_line ?? loc.end_line;
  const end = loc.end_line ?? loc.start_line;
  if (start && end && start !== end) return `${loc.file}:L${start}-L${end}`;
  if (start || end) return `${loc.file}:L${start ?? end}`;
  return loc.file;
}

function fenced(raw: string): string {
  return ["```", raw.trim(), "```"].join("\n");
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}
