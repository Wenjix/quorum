# Agent & contributor notes

Operational guidance for humans and coding agents working in this repo.

## Dependencies / npm

- **Registry:** npmjs, pinned in `.npmrc`. Do not switch to
  `registry.npmmirror.com` or commit a lockfile that resolves from it — its
  advisory endpoint returns `NOT_IMPLEMENTED`, so `npm audit` silently can't run.
- **The `undici` override in `package.json` is an intentional security pin** for
  `@cursor/sdk`'s transitive `@connectrpc/connect-node@1.7.0` dependency — there
  is no compatible upstream `@cursor/sdk` fix yet. Do not remove or loosen it,
  and do not regenerate the lockfile against another registry. The former `tar`
  and `@tootallnate/once` pins were removed after the SDK dropped `sqlite3`.
  Rationale, verification, and the remaining removal condition live in
  `docs/dependencies.md` (tracked in #5).
- **Auditing:** `npm audit --audit-level=moderate` (npmjs registry required).

## Build, test & run

Single Node.js/TypeScript CLI — no server, no watch mode.

- Commands: `npm run build` (tsc), `npm test` (builds, then `node --test dist/test/*.test.js`),
  `npm run typecheck`. `build`/`test` clean `dist/` first (`prebuild`), so stale compiled output
  from a branch switch can't leak into a run.
- After editing `src/`, rebuild and invoke with `node dist/src/cli.js <command>` (no `npm link` step).
- **Offline / no-credentials runs:** `plan-pr <PR-URL>` is plan-only by design — it builds the
  DAG → state → report → Canvas without calling any cloud backend. It still reads the PR's findings from
  GitHub unless you pass them locally with `--scored <clusters.scored.json>`, which makes the run
  fully offline:
  `node dist/src/cli.js plan-pr https://github.com/OWNER/REPO/pull/N --scored clusters.scored.json`.
  On the lower-level `explore`/`triage-pr` commands the same cloud-skip is the `--plan-only` flag.
- **Live commands** — `run-pr`, `post-pr`, `run-dag`, and `triage-pr` without `--plan-only` — call
  the exploration backend and/or GitHub. They need the selected backend's key — `CURSOR_API_KEY`
  (default) or `ANTHROPIC_API_KEY` (with `--provider anthropic` / `QUORUM_PROVIDER=anthropic`) — plus
  an authenticated `gh` CLI. The Anthropic backend also fetches the PR diff for code context, so
  `GITHUB_TOKEN`/`gh` auth improves it but is non-fatal if missing. Without credentials the runner
  still completes, recording per-task `ERROR`/`SKIPPED` instead of crashing.
- **Persisting API keys** — `quorum auth` saves `CURSOR_API_KEY` / `ANTHROPIC_API_KEY` / `QUORUM_PROVIDER`
  to `~/.config/quorum/credentials.json` (0o600) so you don't `export` them every shell. Key resolution
  order: `--api-key` flag > process env > config file. Override the file path with `QUORUM_CONFIG`.
- **Repackaging the skill:** `quorum.skill` is the committed, installable zip of `SKILL.md` +
  `scripts/` + `references/` — the README's install steps unzip it, so a stale zip ships stale
  scripts. After changing any of those files: commit the change, run `npm run build:skill`
  (packages from `HEAD` via `git archive`; `.gitattributes` pins the packaged files to LF so
  Windows `core.autocrlf` can't inject CRLF, and exec bits come from the index), and commit
  the refreshed zip.
- Run artifacts are written under `.quorum/` (gitignored).

## Cursor Cloud

- The cloud-agent environment runs `npm install` on startup (configured in Cursor, not in the repo).
- Use `--no-canvas-mirror` on offline/CI runs so the runner doesn't write into the Cursor managed
  canvases directory under `~/.cursor`.
