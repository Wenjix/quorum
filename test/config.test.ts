import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, test } from "node:test";
import { clearCredentialsCache, credentialsFilePath, loadCredentials, maskSecret, saveCredentials } from "../src/config.js";

let tempDir: string;

function setConfigPath(filename = "credentials.json"): string {
  const path = join(tempDir, filename);
  process.env.QUORUM_CONFIG = path;
  return path;
}

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), "quorum-config-"));
  clearCredentialsCache();
});

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true });
  delete process.env.QUORUM_CONFIG;
  delete process.env.XDG_CONFIG_HOME;
});

describe("credentialsFilePath", () => {
  test("respects QUORUM_CONFIG override", () => {
    setConfigPath();
    assert.equal(credentialsFilePath(), process.env.QUORUM_CONFIG);
  });

  test("uses XDG_CONFIG_HOME when set", () => {
    delete process.env.QUORUM_CONFIG;
    const xdg = join(tempDir, "xdg");
    process.env.XDG_CONFIG_HOME = xdg;
    assert.equal(credentialsFilePath(), join(xdg, "quorum", "credentials.json"));
    delete process.env.XDG_CONFIG_HOME;
  });
});

describe("loadCredentials", () => {
  test("returns empty object when file is missing", () => {
    setConfigPath("nonexistent.json");
    assert.deepEqual(loadCredentials(), {});
  });

  test("reads a valid credentials file", () => {
    const path = setConfigPath();
    writeFileSync(path, JSON.stringify({
      CURSOR_API_KEY: "sk-cursor-123",
      ANTHROPIC_API_KEY: "sk-ant-456",
      QUORUM_PROVIDER: "anthropic",
    }));
    const creds = loadCredentials();
    assert.equal(creds.CURSOR_API_KEY, "sk-cursor-123");
    assert.equal(creds.ANTHROPIC_API_KEY, "sk-ant-456");
    assert.equal(creds.QUORUM_PROVIDER, "anthropic");
  });

  test("caches after first read", () => {
    const path = setConfigPath();
    writeFileSync(path, JSON.stringify({ CURSOR_API_KEY: "first" }));
    assert.equal(loadCredentials().CURSOR_API_KEY, "first");
    // Mutate the file on disk — cached value must still win until cache reset.
    writeFileSync(path, JSON.stringify({ CURSOR_API_KEY: "second" }));
    assert.equal(loadCredentials().CURSOR_API_KEY, "first");
    clearCredentialsCache();
    assert.equal(loadCredentials().CURSOR_API_KEY, "second");
  });

  test("ignores unknown keys and non-string values", () => {
    const path = setConfigPath();
    writeFileSync(path, JSON.stringify({
      CURSOR_API_KEY: "keep",
      EVIL_KEY: "drop",
      ANTHROPIC_API_KEY: 12345,
      QUORUM_PROVIDER: "invalid",
    }));
    const creds = loadCredentials();
    assert.equal(creds.CURSOR_API_KEY, "keep");
    assert.equal(creds.ANTHROPIC_API_KEY, undefined);
    assert.equal(creds.QUORUM_PROVIDER, undefined);
    assert.equal((creds as Record<string, unknown>).EVIL_KEY, undefined);
  });

  test("returns empty object on malformed JSON", () => {
    const path = setConfigPath();
    writeFileSync(path, "{ not valid json");
    assert.deepEqual(loadCredentials(), {});
  });
});

describe("saveCredentials", () => {
  test("writes valid keys and creates parent directories", () => {
    setConfigPath("nested/deep/credentials.json");
    saveCredentials({ CURSOR_API_KEY: "sk-test", QUORUM_PROVIDER: "cursor" });
    clearCredentialsCache();
    const creds = loadCredentials();
    assert.equal(creds.CURSOR_API_KEY, "sk-test");
    assert.equal(creds.QUORUM_PROVIDER, "cursor");
  });

  test("omits empty and undefined values", () => {
    const path = setConfigPath();
    saveCredentials({ CURSOR_API_KEY: "keep", ANTHROPIC_API_KEY: "" });
    const raw = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(raw.CURSOR_API_KEY, "keep");
    assert.equal(raw.ANTHROPIC_API_KEY, undefined);
  });

  test("sets file permissions to 0o600", () => {
    const path = setConfigPath();
    saveCredentials({ CURSOR_API_KEY: "sk-secret" });
    const mode = statSync(path).mode & 0o777;
    assert.equal(mode, 0o600);
  });

  test("updates the in-memory cache", () => {
    setConfigPath();
    saveCredentials({ CURSOR_API_KEY: "cached" });
    // Without clearing the cache, loadCredentials must reflect the saved value.
    assert.equal(loadCredentials().CURSOR_API_KEY, "cached");
  });
});

describe("maskSecret", () => {
  test("returns 'not set' for undefined/empty", () => {
    assert.equal(maskSecret(undefined), "not set");
    assert.equal(maskSecret(""), "not set");
  });

  test("fully masks short secrets", () => {
    assert.equal(maskSecret("abc"), "****");
    assert.equal(maskSecret("12345678"), "****");
  });

  test("shows first 4 and last 4 of long secrets", () => {
    const masked = maskSecret("sk-ant-api03-very-long-key");
    assert.ok(masked.startsWith("sk-a"));
    assert.ok(masked.endsWith("-key"));
    assert.ok(masked.includes("\u2026"));
  });
});
