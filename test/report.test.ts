import assert from "node:assert/strict";
import test from "node:test";
import { withMarker } from "../src/github.js";
import { buildExplorationReport, renderExplorationMarkdown } from "../src/report.js";
import type { RunState } from "../src/types.js";
import { markedResult, sampleScoredDoc } from "./helpers.js";
import { extractMarkedJson } from "../src/json-result.js";

test("renderExplorationMarkdown includes structured root cause and pattern sweep data", () => {
  const doc = sampleScoredDoc();
  const state: RunState = {
    title: "demo",
    startedAt: 1,
    runOutcome: "SUCCESS",
    tasks: [
      {
        id: "root-cause:c1",
        depends_on: [],
        complexity: "HIGH",
        subtask_prompt: "root",
        cluster_id: "c1",
        task_type: "root_cause",
        status: "FINISHED",
        model: "test",
        parsedResult: extractMarkedJson(markedResult("root_cause")).value,
      },
      {
        id: "pattern-sweep:c1",
        depends_on: ["root-cause:c1"],
        complexity: "MED",
        subtask_prompt: "sweep",
        cluster_id: "c1",
        task_type: "pattern_sweep",
        status: "FINISHED",
        model: "test",
        parsedResult: extractMarkedJson(markedResult("pattern_sweep")).value,
      },
    ],
  };

  const report = buildExplorationReport(
    {
      repo: "owner/repo",
      pr: "1",
      repoUrl: "https://github.com/owner/repo",
      prUrl: "https://github.com/owner/repo/pull/1",
      runId: "run",
      generatedAt: "2026-06-12T00:00:00Z",
    },
    doc,
    [doc.clusters[0]],
    state,
  );
  const md = renderExplorationMarkdown(report);

  assert.match(md, /Shared default object is mutated/);
  assert.match(md, /Mechanism/);
  assert.match(md, /src\/other.ts/);
});

test("withMarker adds the idempotency marker", () => {
  assert.match(withMarker("owner/repo", "1", "body"), /quorum:exploration/);
});
