# Agent & contributor notes

Operational guidance for humans and coding agents working in this repo.

## Dependencies / npm

- **Registry:** npmjs, pinned in `.npmrc`. Do not switch to
  `registry.npmmirror.com` or commit a lockfile that resolves from it — its
  advisory endpoint returns `NOT_IMPLEMENTED`, so `npm audit` silently can't run.
- **The `zod` override in `package.json` is an intentional security pin.** `zod` is a
  peer dependency `@cursor/sdk@1.2.0` declares as `^3.25.0 || ^4.0.0`; without the pin npm
  installs the 3.x line, which carries an open advisory fixed only in 4.6.6. Do not remove or
  loosen it, and do not regenerate the lockfile against another registry. The former `undici`,
  `tar` and `@tootallnate/once` pins went away when the SDK dropped `connect-node` and
  `sqlite3`. Rationale, verification, and the removal condition live in
  `docs/dependencies.md` (tracked in #5).
- **Auditing:** `npm audit --audit-level=moderate` (npmjs registry required).

## Build, test & run

Single Node.js/TypeScript CLI — no server, no watch mode.

- Commands: `npm run build` (tsc), `npm test` (builds, then `node --test dist/test/*.test.js`, then
  the python suite under `scripts/tests` with `python3 -m unittest discover -s scripts/tests -v`),
  `npm run typecheck`. `build`/`test` clean `dist/` first (`prebuild`), so stale compiled output
  from a branch switch can't leak into a run.
- **Skill scripts (`scripts/*.py`):** they must import on Python 3.9, so put
  `from __future__ import annotations` in any module that uses PEP 604 unions (`X | None`).
  `scripts/quorum_ledger.py` is the only writer of the PR comment: every other path (the CLI's
  `synthesize`, the skill, an agent following the embedded protocol) posts through it.
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
- **Repackaging the skill:** `quorum.skill` is the committed, installable zip of `SKILL.md`,
  `scripts/` and `references/`. The README's install steps unzip it, so a stale zip ships stale
  scripts. After changing any of those files: commit the change, run `npm run build:skill`
  (it packages from `HEAD` via `git archive`, so uncommitted edits are left out), and commit the
  refreshed zip. `.gitattributes` pins the packaged files to LF so Windows `core.autocrlf` can't
  inject CRLF (exec bits come from the index), and marks `scripts/tests/**` and `__pycache__`
  `export-ignore` so the zip carries no tests. The npm tarball leaves tests out too: the negated
  `!scripts/tests/` and `!scripts/**/__pycache__/` entries in `package.json` `files` do the work
  (a root `.npmignore` alone does not override the `files` whitelist), and `.npmignore` repeats
  them. Check with `npm pack --dry-run`.
- Run artifacts are written under `.quorum/` (gitignored).

## Cursor Cloud

- The cloud-agent environment runs `npm install` on startup (configured in Cursor, not in the repo).
- Use `--no-canvas-mirror` on offline/CI runs so the runner doesn't write into the Cursor managed
  canvases directory under `~/.cursor`.
