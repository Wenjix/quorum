# Agent & contributor notes

Operational guidance for humans and coding agents working in this repo.

## Dependencies / npm

- **Registry:** npmjs, pinned in `.npmrc`. Do not switch to
  `registry.npmmirror.com` or commit a lockfile that resolves from it — its
  advisory endpoint returns `NOT_IMPLEMENTED`, so `npm audit` silently can't run.
- **The `overrides` in `package.json` are intentional security pins** for
  `@cursor/sdk`'s transitive deps (`tar`, `undici`, `@tootallnate/once`) — there
  is no upstream `@cursor/sdk` fix yet. Do not remove or loosen them, and do not
  regenerate the lockfile against another registry. Rationale, verification, and
  removal conditions live in `docs/dependencies.md` (removal tracked in #5).
- **Auditing:** `npm audit --audit-level=moderate` (npmjs registry required).

## Build, test & run

Single Node.js/TypeScript CLI — no server, no watch mode.

- Commands: `npm run build` (tsc), `npm test` (builds, then `node --test dist/test/*.test.js`),
  `npm run typecheck`. `build`/`test` clean `dist/` first (`prebuild`), so stale compiled output
  from a branch switch can't leak into a run.
- After editing `src/`, rebuild and invoke with `node dist/src/cli.js <command>` (no `npm link` step).
- **Offline / no-credentials runs:** `plan-pr <PR-URL>` is plan-only by design — it builds the
  DAG → state → report → Canvas without calling Cursor Cloud. It still reads the PR's findings from
  GitHub unless you pass them locally with `--scored <clusters.scored.json>`, which makes the run
  fully offline:
  `node dist/src/cli.js plan-pr https://github.com/OWNER/REPO/pull/N --scored clusters.scored.json`.
  On the lower-level `explore`/`triage-pr` commands the same cloud-skip is the `--plan-only` flag.
- **Live commands** — `run-pr`, `post-pr`, `run-dag`, and `triage-pr` without `--plan-only` — call
  Cursor Cloud and/or GitHub and need `CURSOR_API_KEY` plus an authenticated `gh` CLI. Without them
  the runner still completes, recording per-task `ERROR`/`SKIPPED` instead of crashing.
- Run artifacts are written under `.quorum/` (gitignored).

## Cursor Cloud

- The cloud-agent environment runs `npm install` on startup (configured in Cursor, not in the repo).
- Use `--no-canvas-mirror` on offline/CI runs so the runner doesn't write into the Cursor managed
  canvases directory under `~/.cursor`.
