export type Complexity = "HIGH" | "MED" | "LOW";

export interface FindingMember {
  id: string;
  reviewer: string;
  file?: string;
  lines?: [number | null, number | null];
  url?: string;
  comment_id?: number;
  node_id?: string;
  outdated?: boolean;
}

export interface ScoredCluster {
  cluster_id: string;
  member_ids: string[];
  canonical_title: string;
  canonical_description?: string;
  category: string;
  severity: "critical" | "major" | "minor" | "nit" | string;
  primary_location?: {
    file?: string;
    start_line?: number | null;
    end_line?: number | null;
  };
  match_type?: string;
  match_confidence?: number;
  match_rationale?: string;
  cross_file?: boolean;
  quorum: number;
  reviewers: string[];
  members: FindingMember[];
}

export interface ScoredClustersDoc {
  generated_at?: string;
  totals: {
    findings: number;
    clusters: number;
    reviewer_denominator: number;
    gate_split?: string[];
  };
  clusters: ScoredCluster[];
}

export interface DagTask {
  id: string;
  depends_on: string[];
  complexity: Complexity;
  subtask_prompt: string;
  cluster_id?: string;
  task_type?: "root_cause" | "pattern_sweep" | string;
}

export interface Dag {
  title: string;
  models?: Partial<Record<Complexity, string>>;
  tasks: DagTask[];
}

export type TaskStatus = "PENDING" | "RUNNING" | "FINISHED" | "ERROR" | "SKIPPED";

export interface TaskState extends DagTask {
  status: TaskStatus;
  model: string;
  startedAt?: number;
  finishedAt?: number;
  durationMs?: number;
  resultText?: string;
  parsedResult?: unknown;
  parseError?: string;
  errorMessage?: string;
  agentId?: string;
  runId?: string;
  requestId?: string;
}

export interface RunState {
  title: string;
  startedAt: number;
  finishedAt?: number;
  runOutcome?: "SUCCESS" | "FAILED" | "INTERRUPTED";
  runMessage?: string;
  tasks: TaskState[];
}

export interface RunnerContext {
  repo: string;
  pr: string;
  repoUrl: string;
  prUrl?: string;
  outDir: string;
  canvasPath?: string | false;
  /** Extra copy of the canvas inside Cursor's managed canvases directory. */
  canvasMirrorPath?: string;
  apiKey?: string;
  concurrency: number;
  taskTimeoutMs: number;
  stream: boolean;
  /** Provider identifier for resolving default models. */
  provider?: "cursor" | "anthropic";
}

export interface TaskExecutionInput {
  task: DagTask;
  prompt: string;
  model: string;
  /** owner/name slug — used by provider-side fetches (e.g. the PR diff). */
  repo: string;
  /** PR number. */
  pr: string;
  repoUrl: string;
  prUrl?: string;
  apiKey?: string;
  timeoutMs: number;
  stream: boolean;
  idempotencyKey: string;
  signal: AbortSignal;
}

export interface TaskExecutionResult {
  status: "finished" | "error" | "cancelled";
  resultText: string;
  durationMs?: number;
  agentId?: string;
  runId?: string;
  requestId?: string;
}

export interface TaskRunnerAdapter {
  runTask(input: TaskExecutionInput): Promise<TaskExecutionResult>;
}

export interface ExplorationContext {
  repo: string;
  pr: string;
  repoUrl: string;
  prUrl?: string;
  runId: string;
  generatedAt: string;
}

export interface ExplorationReport {
  context: ExplorationContext;
  source: {
    reviewer_denominator: number;
    selected_clusters: string[];
  };
  clusters: ClusterExploration[];
  tasks: Array<{
    id: string;
    cluster_id?: string;
    task_type?: string;
    status: TaskStatus;
    agentId?: string;
    runId?: string;
    errorMessage?: string;
    parseError?: string;
  }>;
}

export interface ClusterExploration {
  cluster: ScoredCluster;
  root_cause?: unknown;
  pattern_sweep?: unknown;
  root_cause_raw?: string;
  pattern_sweep_raw?: string;
  warnings: string[];
}
