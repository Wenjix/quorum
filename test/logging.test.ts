import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { loadRunLogs, logEvent } from "../src/logging.js";

test("loadRunLogs() scans .quorum/runs/ recursively when no logDir is given", async () => {
  // Regression: evalCommand once defaulted logDir to ".quorum/log", which forced
  // loadRunLogs down the flat-scan branch and never discovered the per-run logs
  // that live under .quorum/runs/<repo>/<pr>/<ts>/run.log.jsonl. The no-arg call
  // must recurse and find them.
  const work = await mkdtemp(join(tmpdir(), "quorum-logs-"));
  const originalCwd = process.cwd();
  try {
    process.chdir(work);
    await logEvent(join(".quorum", "runs", "owner__repo", "pr-1", "20260101"), {
      type: "dag_start",
      runTitle: "demo run",
    });
    const entries = await loadRunLogs();
    assert.equal(entries.length, 1);
    assert.equal(entries[0].runTitle, "demo run");
  } finally {
    process.chdir(originalCwd);
  }
});

test("loadRunLogs() returns empty when .quorum/runs/ is absent", async () => {
  const work = await mkdtemp(join(tmpdir(), "quorum-logs-empty-"));
  const originalCwd = process.cwd();
  try {
    process.chdir(work);
    const entries = await loadRunLogs();
    assert.deepEqual(entries, []);
  } finally {
    process.chdir(originalCwd);
  }
});

test("loadRunLogs(dir) scans only the given flat directory", async () => {
  const dir = await mkdtemp(join(tmpdir(), "quorum-logs-flat-"));
  await writeFile(
    join(dir, "a.jsonl"),
    JSON.stringify({ type: "task_end", taskId: "t1", status: "FINISHED" }) + "\n",
    "utf8",
  );
  // Non-.jsonl files in the directory are ignored.
  await writeFile(join(dir, "notes.txt"), "ignored", "utf8");

  const entries = await loadRunLogs(dir);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].taskId, "t1");
});
