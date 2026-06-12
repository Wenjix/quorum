# Quorum

Quorum turns noisy automated PR reviews into one prioritized, explainable review brief.

Modern teams often run several AI reviewers on the same pull request: Cursor Bugbot, GitHub Copilot, Devin Reviewer, CodeRabbit, and others. Each reviewer may catch useful issues, but the result is hard to scan, hard to compare, and hard for follow-up agents to use.

Quorum adds a consensus layer. It groups duplicate findings, highlights where multiple reviewers agree, and can launch Cursor Cloud agents to explain root causes and search for similar bug patterns elsewhere in the repository.

## Why It Matters

For reviewers and engineering leads:

- See the issues that multiple reviewers independently found.
- Spend less time reading duplicate comments.
- Understand whether a finding is an isolated bug or a repeated pattern.

For agents and automation:

- Convert review prose into structured JSON.
- Preserve links back to the original review comments.
- Feed downstream agents a clean list of distinct issues instead of raw bot output.

For product teams:

- Make AI review output easier to trust, measure, and improve.
- Build a dataset of which findings were fixed, ignored, or false positives.
- Compare reviewer precision over time by category and quorum level.

## What Quorum Does

Quorum currently has two layers.

1. **Review synthesis skill**
   - Fetches line-anchored bot review comments from a GitHub PR.
   - Clusters comments that describe the same underlying issue.
   - Computes reviewer quorum deterministically.
   - Posts one idempotent synthesis comment back to the PR.

2. **Cursor Cloud exploration runner**
   - Reads the scored Quorum clusters.
   - Selects quorum-backed findings, by default `quorum >= 2`.
   - Runs a small DAG of Cursor Cloud agents for each selected cluster.
   - Produces root-cause analysis, pattern-sweep results, local artifacts, and an optional PR comment.

The cloud runner is explore-only today. It does not create commits, push branches, or open fix PRs.

## Example Output

Quorum posts one synthesis comment that groups automated-review findings into distinct issues, shows reviewer quorum, preserves links back to the original bot comments, and embeds machine-readable JSON for follow-up agents.

![Screenshot of a Quorum review synthesis comment on GitHub, showing three automated-review findings grouped into distinct issues](docs/assets/quorum-synthesis.png)

## How It Works

```text
Bot review comments
        |
        v
Quorum skill
  fetch -> cluster -> validate -> score -> post synthesis
        |
        v
clusters.scored.json
        |
        v
Cloud exploration DAG
  root cause -> pattern sweep -> report
        |
        v
exploration.md + exploration.json + Canvas + optional PR comment
```

## Quick Start

### Requirements

- Node.js 22 or newer
- `npm`
- `gh` CLI, authenticated for the target GitHub repo
- `jq` and `python3` for the skill scripts
- `CURSOR_API_KEY` for live Cursor Cloud exploration

Install dependencies and build:

```bash
npm install
npm run build
```

Run tests:

```bash
npm test
```

## Use the Quorum Skill

The packaged skill is `quorum.skill`. It can be installed into Claude Code or Cursor.

For Claude Code:

```bash
mkdir -p .claude/skills
unzip quorum.skill -d .claude/skills
```

For Cursor:

```bash
mkdir -p .cursor/skills
unzip quorum.skill -d .cursor/skills
```

Then open an agent session on a PR branch and ask:

```text
Use Quorum to triage the bot reviews on this PR.
```

The skill produces a `clusters.scored.json` file. That file is the input to the Phase 2 cloud runner.

## Use the Cursor Cloud Runner

First set your Cursor API key:

```bash
export CURSOR_API_KEY="crsr_..."
```

Generate the exploration DAG and report shell without making Cursor Cloud or GitHub calls:

```bash
node dist/src/cli.js explore \
  --repo OWNER/REPO \
  --pr 123 \
  --scored path/to/clusters.scored.json \
  --plan-only
```

Run live Cursor Cloud exploration but do not post to GitHub:

```bash
node dist/src/cli.js explore \
  --repo OWNER/REPO \
  --pr 123 \
  --scored path/to/clusters.scored.json \
  --no-post
```

Run live exploration and upsert the PR exploration comment:

```bash
node dist/src/cli.js explore \
  --repo OWNER/REPO \
  --pr 123 \
  --scored path/to/clusters.scored.json
```

Useful options:

```bash
--cluster ID[,ID]        Explore specific cluster IDs.
--min-quorum N           Default: 2.
--max-clusters N         Limit selected clusters.
--out DIR                Write artifacts to a custom directory.
--concurrency N          Default: 4.
--task-timeout-ms N      Default: 1200000.
--dry-run | --no-post    Do not write a PR comment.
--plan-only              Do not call Cursor Cloud or GitHub.
--no-stream              Wait for final agent results only.
--canvas-path PATH       Write the Cursor Canvas artifact to this path.
--no-canvas              Skip Canvas generation.
```

## Outputs

Each run writes artifacts under `.quorum/runs/<timestamp>-pr<N>/` by default.

- `input.clusters.scored.json`: the Quorum synthesis input
- `dag.json`: the exploration graph
- `state.json`: task status, model, run ID, agent ID, errors, and parsed results
- `exploration.json`: structured root-cause and pattern-sweep output
- `exploration.md`: human-readable report
- `quorum-exploration.canvas.tsx`: Cursor Canvas artifact for visual DAG status and task summaries

Open the `.canvas.tsx` file in Cursor to view the DAG as an interactive Canvas. During a live run, Quorum rewrites the file whenever task state changes, so Cursor can refresh the task cards as root-cause and pattern-sweep agents finish.

When posting is enabled, Quorum upserts one PR comment using the hidden marker:

```html
<!-- quorum:exploration -->
```

Reruns update the existing exploration comment instead of spamming the PR.

## Safety Defaults

Quorum treats reviewer consensus as signal, not truth.

- Clustering is conservative: when uncertain, keep findings separate.
- Quorum is computed in code from distinct reviewers, not by the model.
- Cursor Cloud exploration is read-only by prompt and by configuration.
- The runner sets `autoCreatePR: false` and does not ask cloud agents to edit files.
- Pattern sweeps classify likely and possible repeats; they do not apply fixes.

## Repository Layout

```text
SKILL.md                 Skill instructions for review synthesis
quorum.skill             Packaged Claude/Cursor skill
src/                     TypeScript cloud runner
test/                    Node test suite
package.json             Build, test, and dependency metadata
```

Key source modules:

- `src/cli.ts`: command-line interface
- `src/dag.ts`: DAG validation and rank computation
- `src/runner.ts`: task execution and state persistence
- `src/canvas.ts`: Cursor Canvas rendering
- `src/cursor-cloud-adapter.ts`: Cursor SDK integration
- `src/report.ts`: JSON and Markdown exploration reports
- `src/github.ts`: idempotent PR comment upsert

## Development

Run type checks:

```bash
npm run typecheck
```

Run the full test suite:

```bash
npm test
```

Build the CLI:

```bash
npm run build
```

After building, the CLI entrypoint is:

```bash
node dist/src/cli.js --help
```

## Current Status

Quorum is built for dogfooding.

It is not yet a hosted GitHub App. The fastest path is still interactive use: run the skill on a PR, inspect the synthesis, then launch cloud exploration for the quorum-backed findings.

Planned next steps:

- Preserve dogfood logs for reviewer precision analysis.
- Add richer evaluation around bad merges and false positives.
- Add optional fix-draft agents after the explore-only flow is reliable.
- Package hosted automation through GitHub Actions or a GitHub App.
