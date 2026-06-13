# AGENTS.md

## Cursor Cloud specific instructions

Quorum is a single Node.js/TypeScript CLI (no long-running server). The `npm install` dependency
refresh runs automatically on startup; the notes below are the non-obvious bits for running it.

- Standard commands live in `package.json` and `README.md`: `npm run build`, `npm test`
  (build + `node --test`), `npm run typecheck`. There is no `npm run dev`/watch mode — rebuild with
  `npm run build` after editing `src/`, then run `node dist/src/cli.js ...`.
- `npm test` runs against the compiled `dist/` output, so it builds first. If tests behave oddly
  after a checkout, delete `dist/` and rebuild.
- Run the CLI without `npm link` via `node dist/src/cli.js <command>`.
- Live commands (`run-pr`, `post-pr`, `explore` without `--plan-only`, `run-dag`) call Cursor Cloud
  and/or GitHub. They require `CURSOR_API_KEY` and an authenticated `gh` CLI. Without these, the
  runner still completes and just records per-task `ERROR`/`SKIPPED` status instead of crashing.
- Gotcha: `plan-pr` is documented as plan-only, but the runner only skips the live Cursor Cloud call
  when the explicit `--plan-only` flag is present. To exercise the full offline flow (DAG + state +
  report + Canvas) with no API key or network, pass `--plan-only` and a local
  `--scored <clusters.scored.json>`, e.g.
  `node dist/src/cli.js plan-pr https://github.com/OWNER/REPO/pull/N --scored clusters.scored.json --plan-only`.
- Use `--no-canvas-mirror` for offline/test runs to avoid writing into the Cursor managed canvases
  directory under `~/.cursor`.
- Run artifacts are written under `.quorum/` (gitignored).
