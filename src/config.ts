import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export interface Credentials {
  CURSOR_API_KEY?: string;
  ANTHROPIC_API_KEY?: string;
  QUORUM_PROVIDER?: "cursor" | "anthropic";
}

const ALLOWED_KEYS = ["CURSOR_API_KEY", "ANTHROPIC_API_KEY", "QUORUM_PROVIDER"] as const;

/**
 * Resolve the credentials file path.
 *
 * Resolution order:
 *   1. QUORUM_CONFIG env var (explicit override — also used by tests)
 *   2. $XDG_CONFIG_HOME/quorum/credentials.json
 *   3. ~/.config/quorum/credentials.json  (fallback)
 *
 * `homedir()` respects $HOME on Unix and %USERPROFILE% on Windows, so this is
 * cross-platform without manual env fallbacks.
 */
export function credentialsFilePath(): string {
  if (process.env.QUORUM_CONFIG) return process.env.QUORUM_CONFIG;
  const xdg = process.env.XDG_CONFIG_HOME;
  const base = xdg ? join(xdg, "quorum") : join(homedir(), ".config", "quorum");
  return join(base, "credentials.json");
}

let cache: Credentials | undefined;

/** Reset the in-memory cache. Intended for tests. */
export function clearCredentialsCache(): void {
  cache = undefined;
}

/**
 * Load credentials from disk (cached after first call).
 *
 * Returns an empty object when the file is missing or malformed — never throws
 * — so callers can treat it as a best-effort fallback below process.env.
 */
export function loadCredentials(): Credentials {
  if (cache) return cache;
  const path = credentialsFilePath();
  try {
    const raw = readFileSync(path, "utf8");
    cache = sanitize(JSON.parse(raw));
  } catch {
    cache = {};
  }
  return cache;
}

/**
 * Persist credentials to disk with restrictive permissions (0o600).
 * Writes the parent directory if it does not exist.
 */
export function saveCredentials(creds: Credentials): void {
  const path = credentialsFilePath();
  mkdirSync(dirname(path), { recursive: true });
  const compact: Record<string, string> = {};
  for (const key of ALLOWED_KEYS) {
    const value = creds[key];
    if (typeof value === "string" && value.length > 0) {
      compact[key] = value;
    }
  }
  writeFileSync(path, `${JSON.stringify(compact, null, 2)}\n`, "utf8");
  try {
    chmodSync(path, 0o600);
  } catch {
    // Best-effort: chmod can fail on Windows or non-POSIX filesystems.
  }
  cache = { ...compact };
}

/** Mask a secret for display: first 4 + ellipsis + last 4 characters. */
export function maskSecret(secret: string | undefined): string {
  if (!secret) return "not set";
  if (secret.length <= 8) return "****";
  return `${secret.slice(0, 4)}\u2026${secret.slice(-4)}`;
}

function sanitize(value: unknown): Credentials {
  if (typeof value !== "object" || value === null) return {};
  const result: Credentials = {};
  const record = value as Record<string, unknown>;
  for (const key of ALLOWED_KEYS) {
    const v = record[key];
    if (typeof v !== "string" || v.length === 0) continue;
    if (key === "QUORUM_PROVIDER" && v !== "cursor" && v !== "anthropic") continue;
    (result as Record<string, string>)[key] = v;
  }
  return result;
}
