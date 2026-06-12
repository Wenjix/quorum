import type { Dag, ScoredCluster, ScoredClustersDoc } from "./types.js";

export interface ExplorationDagOptions {
  repo: string;
  pr: string;
  selectedClusters: ScoredCluster[];
  scoredDoc: ScoredClustersDoc;
}

export function buildExplorationDag(options: ExplorationDagOptions): Dag {
  const tasks = options.selectedClusters.flatMap((cluster) => {
    const rootId = `root-cause:${cluster.cluster_id}`;
    const sweepId = `pattern-sweep:${cluster.cluster_id}`;
    return [
      {
        id: rootId,
        depends_on: [],
        complexity: "HIGH" as const,
        task_type: "root_cause",
        cluster_id: cluster.cluster_id,
        subtask_prompt: buildRootCausePrompt(options, cluster),
      },
      {
        id: sweepId,
        depends_on: [rootId],
        complexity: "MED" as const,
        task_type: "pattern_sweep",
        cluster_id: cluster.cluster_id,
        subtask_prompt: buildPatternSweepPrompt(options, cluster),
      },
    ];
  });

  return {
    title: `Quorum exploration for ${options.repo}#${options.pr}`,
    tasks,
  };
}

function buildRootCausePrompt(options: ExplorationDagOptions, cluster: ScoredCluster): string {
  return [
    "You are a read-only PR review exploration agent for Quorum.",
    "",
    `Repository: ${options.repo}`,
    `Pull request: #${options.pr}`,
    "",
    "Investigate the clustered automated-review finding below. Explain the root cause, the invariant or contract that is missing, and the concrete evidence in the code. Do not edit files, run formatters, create commits, push branches, or open pull requests.",
    "",
    "Cluster input:",
    "```json",
    JSON.stringify(clusterContext(options, cluster), null, 2),
    "```",
    "",
    "Return a concise human-readable explanation, then end with exactly one fenced JSON block using this shape:",
    "```json",
    JSON.stringify(
      {
        QUORUM_TASK_RESULT: true,
        task_type: "root_cause",
        cluster_id: cluster.cluster_id,
        summary: "one sentence",
        mechanism: "how the bug happens",
        missing_invariant: "what should have been true",
        evidence: [{ file: "path", lines: "L1-L2", note: "why this matters" }],
        confidence: "high | medium | low",
        follow_up_questions: ["optional"],
      },
      null,
      2,
    ),
    "```",
  ].join("\n");
}

function buildPatternSweepPrompt(options: ExplorationDagOptions, cluster: ScoredCluster): string {
  return [
    "You are a read-only pattern-sweep agent for Quorum.",
    "",
    `Repository: ${options.repo}`,
    `Pull request: #${options.pr}`,
    "",
    "Use the upstream root-cause result as the source of truth. Search the repository for the same bug pattern or missing invariant outside the original finding. Classify each candidate as likely, possible, or unlikely. Do not edit files, run formatters, create commits, push branches, or open pull requests.",
    "",
    "Original cluster input:",
    "```json",
    JSON.stringify(clusterContext(options, cluster), null, 2),
    "```",
    "",
    "Return a concise human-readable summary, then end with exactly one fenced JSON block using this shape:",
    "```json",
    JSON.stringify(
      {
        QUORUM_TASK_RESULT: true,
        task_type: "pattern_sweep",
        cluster_id: cluster.cluster_id,
        search_strategy: ["grep/query/pattern used"],
        matches: [
          {
            file: "path",
            lines: "L1-L2",
            risk: "likely | possible | unlikely",
            rationale: "why this does or does not match",
          },
        ],
        recommended_actions: ["optional"],
      },
      null,
      2,
    ),
    "```",
  ].join("\n");
}

function clusterContext(options: ExplorationDagOptions, cluster: ScoredCluster): unknown {
  return {
    reviewer_denominator: options.scoredDoc.totals.reviewer_denominator,
    cluster,
  };
}
