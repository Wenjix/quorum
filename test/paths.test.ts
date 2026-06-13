import assert from "node:assert/strict";
import { join } from "node:path";
import test from "node:test";
import { scriptsDirFromModuleUrl } from "../src/paths.js";

test("scriptsDirFromModuleUrl resolves the package root three levels up", () => {
  const result = scriptsDirFromModuleUrl("file:///opt/quorum/dist/src/cli.js");
  assert.equal(result, join("/opt/quorum", "scripts"));
});

test("scriptsDirFromModuleUrl decodes percent-encoded characters", () => {
  // Regression: a raw url.replace("file://", "") left %20 undecoded, so an
  // install path with a space (e.g. macOS "Application Support") pointed at a
  // nonexistent scripts/ dir. fileURLToPath decodes it.
  const result = scriptsDirFromModuleUrl("file:///home/John%20Smith/quorum/dist/src/cli.js");
  assert.ok(result.includes("John Smith"), result);
  assert.ok(!result.includes("%20"), result);
  assert.ok(result.endsWith(join("quorum", "scripts")), result);
});

test("scriptsDirFromModuleUrl falls back to cwd for non-file URLs", () => {
  const result = scriptsDirFromModuleUrl("data:text/javascript,export%20const%20x=1");
  assert.equal(result, join(process.cwd(), "scripts"));
});
