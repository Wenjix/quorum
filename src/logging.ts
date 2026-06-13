import { appendFile, mkdir, readdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import type { Dirent } from "node:fs";
import type { RunState, TaskState } from "./types.js";

export interface LogEntry {
  ts?: string;
  type: "dag_start" | "dag_end" | "task_start" | "task_end" | "task_error" | "task_skip";
  runTitle?: string;
  taskId?: string;
  taskType?: string;
  clusterId?: string;
  status?: string;
  errorMessage?: string;
  parseError?: string;
  durationMs?: number;
  agentId?: string;
  runId?: string;
}

/** Write a timestamped log entry to run.log.jsonl in the output directory. */
export async function logEvent(outDir: string, entry: LogEntry): Promise<void> {
  await mkdir(outDir, { recursive: true });
  const enriched = { ts: new Date().toISOString(), ...entry };
  await appendFile(join(outDir, "run.log.jsonl"), JSON.stringify(enriched) + "\n", "utf8");
}

/** Log dag_start for an entire run. */
export async function logRunStart(outDir: string, state: RunState): Promise<void> {
  await logEvent(outDir, {
    type: "dag_start",
    runTitle: state.title,
  });
}

/** Log dag_end after the run completes. */
export async function logRunEnd(
  outDir: string,
  state: RunState,
): Promise<void> {
  await logEvent(outDir, {
    type: "dag_end",
    runTitle: state.title,
    status: state.runOutcome ?? "UNKNOWN",
    errorMessage: state.runMessage,
  });
}

/** Log an individual task lifecycle event. */
export async function logTaskEvent(outDir: string, task: TaskState): Promise<void> {
  let type: LogEntry["type"] = "task_end";
  if (task.status === "ERROR") type = "task_error";
  else if (task.status === "SKIPPED") type = "task_skip";
  else if (task.status === "RUNNING") type = "task_start";

  await logEvent(outDir, {
    type,
    taskId: task.id,
    taskType: task.task_type,
    clusterId: task.cluster_id,
    status: task.status,
    errorMessage: task.errorMessage,
    parseError: task.parseError,
    durationMs: task.durationMs,
    agentId: task.agentId,
    runId: task.runId,
  });
}

/**
 * Accumulate all run.log.jsonl files from the given directory tree.
 * When `logDir` is provided, scans that directory only (flat .jsonl files).
 * When `logDir` is omitted, scans .quorum/runs/ recursively for per-run log files.
 * Returns an empty array if no log files are found.
 */
export async function loadRunLogs(
  logDir?: string,
): Promise<LogEntry[]> {
  const entries: LogEntry[] = [];

  if (logDir) {
    // Scan only the explicitly requested directory
    try {
      const files = await readdir(logDir);
      for (const file of files) {
        if (!file.endsWith(".jsonl")) continue;
        try {
          const content = await readFile(join(logDir, file), "utf8");
          entries.push(...parseLogLines(content));
        } catch {
          // Skip unreadable files
        }
      }
    } catch {
      // logDir may not exist
    }
  } else {
    // Default: scan .quorum/runs/ recursively
    try {
      await collectRunDirLogs(join(".quorum", "runs"), entries);
    } catch {
      // .quorum/runs may not exist
    }
  }

  return entries;
}

async function collectRunDirLogs(
  dir: string,
  entries: LogEntry[],
): Promise<void> {
  let items: Dirent[] = [];
  try {
    items = await readdir(dir, { withFileTypes: true });
  } catch {
    return;
  }

  for (const item of items) {
    const fullPath = join(dir, item.name);
    if (item.isDirectory()) {
      await collectRunDirLogs(fullPath, entries);
    } else if (item.name === "run.log.jsonl") {
      try {
        const content = await readFile(fullPath, "utf8");
        entries.push(...parseLogLines(content));
      } catch {
        // Skip unreadable files
      }
    }
  }
}

function parseLogLines(content: string): LogEntry[] {
  const entries: LogEntry[] = [];
  for (const line of content.split("\n")) {
    if (!line.trim()) continue;
    try {
      entries.push(JSON.parse(line) as LogEntry);
    } catch {
      // Skip malformed lines
    }
  }
  return entries;
}
