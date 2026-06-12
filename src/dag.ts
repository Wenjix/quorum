import type { Complexity, Dag, DagTask } from "./types.js";

export const DEFAULT_MODEL_MAP: Record<Complexity, string> = {
  HIGH: "gpt-5.3-codex",
  MED: "composer-2",
  LOW: "auto-low",
};

const COMPLEXITIES = new Set<Complexity>(["HIGH", "MED", "LOW"]);

export function parseDag(raw: unknown): Dag {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("DAG must be a JSON object.");
  }
  const obj = raw as Record<string, unknown>;
  if (typeof obj.title !== "string" || obj.title.trim() === "") {
    throw new Error("DAG.title must be a non-empty string.");
  }
  if (!Array.isArray(obj.tasks) || obj.tasks.length === 0) {
    throw new Error("DAG.tasks must be a non-empty array.");
  }

  const tasks = obj.tasks.map((task, index) => validateTask(task, index));
  const ids = new Set<string>();
  for (const task of tasks) {
    if (ids.has(task.id)) {
      throw new Error(`Duplicate task id: ${task.id}`);
    }
    ids.add(task.id);
  }
  for (const task of tasks) {
    for (const dep of task.depends_on) {
      if (!ids.has(dep)) {
        throw new Error(`Task ${task.id} depends_on unknown id: ${dep}`);
      }
      if (dep === task.id) {
        throw new Error(`Task ${task.id} depends on itself.`);
      }
    }
  }
  detectCycle(tasks);

  return {
    title: obj.title.trim(),
    models: validateModelMap(obj.models),
    tasks,
  };
}

export function validateModelMap(raw: unknown): Partial<Record<Complexity, string>> | undefined {
  if (raw === undefined) return undefined;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("DAG.models must be a JSON object.");
  }
  const models: Partial<Record<Complexity, string>> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!COMPLEXITIES.has(key as Complexity)) {
      throw new Error(`DAG.models contains unknown complexity key: ${key}`);
    }
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(`DAG.models.${key} must be a non-empty string.`);
    }
    models[key as Complexity] = value.trim();
  }
  return models;
}

export function computeRanks(dag: Dag): DagTask[][] {
  const remaining = new Map<string, number>();
  const byId = new Map<string, DagTask>();
  const dependents = new Map<string, DagTask[]>();

  for (const task of dag.tasks) {
    remaining.set(task.id, task.depends_on.length);
    byId.set(task.id, task);
    dependents.set(task.id, []);
  }
  for (const task of dag.tasks) {
    for (const dep of task.depends_on) {
      dependents.get(dep)?.push(task);
    }
  }

  const ranks: DagTask[][] = [];
  let frontier = dag.tasks.filter((task) => remaining.get(task.id) === 0);
  while (frontier.length > 0) {
    ranks.push(frontier);
    const next: DagTask[] = [];
    for (const task of frontier) {
      for (const child of dependents.get(task.id) ?? []) {
        const nextRemaining = (remaining.get(child.id) ?? 0) - 1;
        remaining.set(child.id, nextRemaining);
        if (nextRemaining === 0) {
          next.push(byId.get(child.id)!);
        }
      }
    }
    frontier = next;
  }

  const placed = ranks.reduce((sum, rank) => sum + rank.length, 0);
  if (placed !== dag.tasks.length) {
    throw new Error("Topological sort failed; DAG likely contains a cycle.");
  }
  return ranks;
}

export function createModelResolver(
  overrides: Partial<Record<Complexity, string>> | undefined,
): (complexity: Complexity) => string {
  const models = { ...DEFAULT_MODEL_MAP, ...(overrides ?? {}) };
  return (complexity) => models[complexity];
}

function validateTask(raw: unknown, index: number): DagTask {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`tasks[${index}] must be an object.`);
  }
  const task = raw as Record<string, unknown>;
  if (typeof task.id !== "string" || task.id.trim() === "") {
    throw new Error(`tasks[${index}].id must be a non-empty string.`);
  }
  const dependsOn = task.depends_on ?? [];
  if (!Array.isArray(dependsOn) || dependsOn.some((dep) => typeof dep !== "string")) {
    throw new Error(`tasks[${index}].depends_on must be an array of strings.`);
  }
  if (typeof task.complexity !== "string" || !COMPLEXITIES.has(task.complexity as Complexity)) {
    throw new Error(`tasks[${index}].complexity must be one of HIGH | MED | LOW.`);
  }
  if (typeof task.subtask_prompt !== "string" || task.subtask_prompt.trim() === "") {
    throw new Error(`tasks[${index}].subtask_prompt must be a non-empty string.`);
  }

  return {
    id: task.id.trim(),
    depends_on: [...new Set(dependsOn as string[])],
    complexity: task.complexity as Complexity,
    subtask_prompt: task.subtask_prompt.trim(),
    cluster_id: typeof task.cluster_id === "string" ? task.cluster_id : undefined,
    task_type: typeof task.task_type === "string" ? task.task_type : undefined,
  };
}

function detectCycle(tasks: DagTask[]): void {
  const adj = new Map<string, string[]>();
  for (const task of tasks) adj.set(task.id, []);
  for (const task of tasks) {
    for (const dep of task.depends_on) adj.get(dep)?.push(task.id);
  }

  const color = new Map<string, 0 | 1 | 2>();
  for (const task of tasks) color.set(task.id, 0);

  const visit = (id: string, path: string[]): void => {
    color.set(id, 1);
    for (const child of adj.get(id) ?? []) {
      const childColor = color.get(child) ?? 0;
      if (childColor === 1) {
        const cycleStart = path.indexOf(child);
        const cycle = [...path.slice(cycleStart), child].join(" -> ");
        throw new Error(`Cycle detected: ${cycle}`);
      }
      if (childColor === 0) visit(child, [...path, child]);
    }
    color.set(id, 2);
  };

  for (const task of tasks) {
    if (color.get(task.id) === 0) visit(task.id, [task.id]);
  }
}
