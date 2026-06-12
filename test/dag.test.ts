import assert from "node:assert/strict";
import test from "node:test";
import { computeRanks, parseDag } from "../src/dag.js";

test("parseDag validates and ranks a diamond DAG", () => {
  const dag = parseDag({
    title: "demo",
    tasks: [
      { id: "a", depends_on: [], complexity: "LOW", subtask_prompt: "a" },
      { id: "b", depends_on: [], complexity: "LOW", subtask_prompt: "b" },
      { id: "c", depends_on: ["a", "b"], complexity: "MED", subtask_prompt: "c" },
    ],
  });

  assert.deepEqual(
    computeRanks(dag).map((rank) => rank.map((task) => task.id)),
    [["a", "b"], ["c"]],
  );
});

test("parseDag rejects duplicate ids, unknown deps, and cycles", () => {
  assert.throws(
    () =>
      parseDag({
        title: "dup",
        tasks: [
          { id: "a", depends_on: [], complexity: "LOW", subtask_prompt: "a" },
          { id: "a", depends_on: [], complexity: "LOW", subtask_prompt: "a2" },
        ],
      }),
    /Duplicate task id/,
  );

  assert.throws(
    () =>
      parseDag({
        title: "unknown",
        tasks: [{ id: "a", depends_on: ["b"], complexity: "LOW", subtask_prompt: "a" }],
      }),
    /depends_on unknown id/,
  );

  assert.throws(
    () =>
      parseDag({
        title: "cycle",
        tasks: [
          { id: "a", depends_on: ["b"], complexity: "LOW", subtask_prompt: "a" },
          { id: "b", depends_on: ["a"], complexity: "LOW", subtask_prompt: "b" },
        ],
      }),
    /Cycle detected/,
  );
});
