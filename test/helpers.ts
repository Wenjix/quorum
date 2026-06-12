import type {
  ScoredClustersDoc,
  TaskExecutionInput,
  TaskExecutionResult,
  TaskRunnerAdapter,
} from "../src/types.js";

export function sampleScoredDoc(): ScoredClustersDoc {
  return {
    generated_at: "2026-06-12T00:00:00Z",
    totals: {
      findings: 3,
      clusters: 2,
      reviewer_denominator: 3,
      gate_split: [],
    },
    clusters: [
      {
        cluster_id: "c1",
        member_ids: ["bugbot-1", "devin-1"],
        canonical_title: "Shared default object is mutated",
        canonical_description: "Object.assign mutates defaults and leaks options across calls.",
        category: "logic",
        severity: "major",
        primary_location: {
          file: "src/options.ts",
          start_line: 12,
          end_line: 14,
        },
        match_type: "same-root-cause",
        match_confidence: 0.9,
        cross_file: false,
        quorum: 2,
        reviewers: ["bugbot", "devin"],
        members: [
          {
            id: "bugbot-1",
            reviewer: "bugbot",
            file: "src/options.ts",
            lines: [12, 14],
            url: "https://example.test/comment/1",
          },
          {
            id: "devin-1",
            reviewer: "devin",
            file: "src/routes.ts",
            lines: [88, 88],
            url: "https://example.test/comment/2",
          },
        ],
      },
      {
        cluster_id: "c2",
        member_ids: ["copilot-1"],
        canonical_title: "Missing help text punctuation",
        category: "style",
        severity: "nit",
        primary_location: {
          file: "README.md",
          start_line: 4,
          end_line: 4,
        },
        match_type: "singleton",
        match_confidence: 1,
        cross_file: false,
        quorum: 1,
        reviewers: ["copilot"],
        members: [
          {
            id: "copilot-1",
            reviewer: "copilot",
            file: "README.md",
            lines: [4, 4],
            url: "https://example.test/comment/3",
          },
        ],
      },
    ],
  };
}

export class FakeAdapter implements TaskRunnerAdapter {
  readonly prompts = new Map<string, string>();
  constructor(
    private readonly responses: Record<
      string,
      TaskExecutionResult | Error | ((input: TaskExecutionInput) => Promise<TaskExecutionResult>)
    >,
  ) {}

  async runTask(input: TaskExecutionInput): Promise<TaskExecutionResult> {
    this.prompts.set(input.task.id, input.prompt);
    const response = this.responses[input.task.id];
    if (!response) throw new Error(`no fake response for ${input.task.id}`);
    if (response instanceof Error) throw response;
    if (typeof response === "function") return response(input);
    return response;
  }
}

export function markedResult(taskType: string, clusterId = "c1"): string {
  return [
    "done",
    "```json",
    JSON.stringify({
      QUORUM_TASK_RESULT: true,
      task_type: taskType,
      cluster_id: clusterId,
      summary: "root summary",
      mechanism: "mechanism",
      missing_invariant: "invariant",
      evidence: [{ file: "src/options.ts", lines: "L12-L14", note: "note" }],
      matches: [{ file: "src/other.ts", lines: "L2", risk: "possible", rationale: "same call" }],
      recommended_actions: ["inspect match"],
    }),
    "```",
  ].join("\n");
}
