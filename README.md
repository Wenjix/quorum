# Quorum

**One triage comment for your automated PR reviews.**

Quorum groups duplicate findings from review bots on a GitHub pull request (PR).
It counts distinct reviewers, directs agents to examine each issue, and records the results in one comment.
The comment gives people a review summary and gives agents structured JSON.

[Quick start](#quick-start) · [Use the skill](#use-the-quorum-skill) · [Cloud runner](#use-the-cloud-runner) · [Outputs](#outputs) · [Development](#development)

## Choose a workflow

| Workflow | Purpose |
| --- | --- |
| **[Quorum skill](#use-the-quorum-skill)** | Group findings, direct triage agents, and maintain the PR comment in Claude Code or Cursor. |
| **[Cloud runner](#use-the-cloud-runner)** | Use Cursor Cloud or Anthropic to examine root causes and search for similar bugs. |

The cloud runner is optional. It produces reports and a Cursor Canvas.
It does not create commits, push branches, or open PRs with fixes.

## How it works

1. **Collect.** Read bot comments that refer to lines in the PR.
2. **Group.** Put findings about the same issue into a cluster.
3. **Count.** Calculate the number of distinct reviewers for each cluster. This count is its **quorum**.
4. **Triage.** Assign each cluster to a lane. Direct agents to examine the issues.
5. **Record.** Update the PR comment with evidence, decisions, commits, and tests.

| Lane | Reviewer count | Agent task |
| --- | --- | --- |
| `agreed` | Two or more | Treat the issue as real. Identify the missing code rule, search for similar bugs, and propose a fix. |
| `solo` | One | Examine the evidence. Accept or reject the finding. |

Quorum calculates the count and lane in code. Agreement between reviewers is evidence, but it does not prove that an issue exists.
Triage agents make no changes to tracked files. The owner decides which fixes to apply.

## Quick start

### Requirements

| Component | Requirement |
| --- | --- |
| CLI | Node.js 22.13 or newer and npm |
| GitHub access | An authenticated `gh` CLI with access to the target repository |
| Skill scripts | `jq` and Python 3.9 or newer |
| Live cloud runs | A Cursor Cloud or Anthropic API key |

The script examples use `python3`. On Windows, use `python` if necessary.
A local plan needs no API key. It also needs no GitHub access when you supply a scored file.

### Build the CLI

Run these commands from this repository:

```bash
npm install
npm run build
```

Link the CLI to use `quorum` from any directory:

```bash
npm link
```

Without `npm link`, replace `quorum` with `node dist/src/cli.js` from this repository.

Check the requirements:

```bash
quorum setup
```

## Use the Quorum skill

Install [quorum.skill](quorum.skill) into your project. Choose the command for your editor.
Run the command from the directory that contains `quorum.skill`.

**Claude Code**

```bash
mkdir -p .claude/skills
unzip quorum.skill -d .claude/skills
```

**Cursor**

```bash
mkdir -p .cursor/skills
unzip quorum.skill -d .cursor/skills
```

Open an agent session on the PR branch. Give the agent this instruction:

```text
Use Quorum to triage the bot reviews on this PR.
```

The skill groups the findings, calculates quorum, and directs agents to examine both lanes.
It maintains one triage comment as the review progresses.
A **disposition** records a cluster's decision, evidence, and any fix details.

To keep results in the conversation, tell the skill `--no-comment`.
This is a skill instruction, not a script flag. Without a posted comment, the ledger commands cannot record results.

<details>
<summary><strong>Skill steps and script commands</strong></summary>

See [SKILL.md](SKILL.md) for the full procedure. Script paths below are relative to the installed skill directory.

1. **Collect findings.** Run `scripts/fetch_findings.sh OWNER/REPO PR findings.json`.
   Stop if no findings match. Set `QUORUM_BOTS` if the default filter misses a reviewer.
2. **Group findings.** Use [the clustering rubric](references/clustering-rubric.md) to write `clusters.json`.
   With one distinct reviewer, skip this step. Each finding becomes a separate `solo` cluster.
3. **Check the groups.** Run the command below to calculate quorum and lanes.
   After two failed checks, use `--all-singletons` in place of `clusters.json`.
4. **Post the plan.** Use `quorum_ledger.py post-plan`. Add `--dry-run` to preview the comment.
5. **Direct the agents.** Run one escalation agent per `agreed` cluster.
   Run one validator agent per batch of up to four `solo` clusters. The agents run in parallel without changes to tracked files.
6. **Record the results.** Use `escalate` for an agreed cluster. Use `dispose` for a decision or fix.
   An invalid result produces a field-specific error without a write.

```bash
python3 scripts/validate_partition.py findings.json clusters.json \
  -o clusters.scored.json

python3 scripts/quorum_ledger.py post-plan OWNER/REPO PR clusters.scored.json

python3 scripts/quorum_ledger.py escalate OWNER/REPO PR CLUSTER_ID \
  --from escalation.json

python3 scripts/quorum_ledger.py dispose OWNER/REPO PR \
  --from dispositions.json --reply
```

After an approved fix, record the commit and tests:

```bash
python3 scripts/quorum_ledger.py dispose OWNER/REPO PR CLUSTER_ID \
  --commit SHA --tests "what ran" --reply
```

If the owner declines a fix, record that decision:

```bash
python3 scripts/quorum_ledger.py dispose OWNER/REPO PR CLUSTER_ID \
  --verdict deferred --note "owner's reason" --by owner
```

An owner deferral stops that cluster from blocking the merge gate.

</details>

<details>
<summary><strong>CLI synthesis and reviewer history</strong></summary>

Run synthesis without the skill:

```bash
quorum synthesize OWNER/REPO#123
```

This command collects findings, calculates quorum, and posts the plan through `quorum_ledger.py`.
It uses one cluster per finding unless you supply `clusters.json` with `--clusters PATH`.
Add `--dry-run` to preview the comment. Add `--no-post` to omit the comment.

Calculate reviewer precision from earlier ledgers:

```bash
quorum priors OWNER/REPO
```

These **priors** describe each reviewer's past precision in this repository.
A reviewer needs at least five judged findings. A prior does not decide whether a new finding is valid.

The default file is `.quorum/priors/<owner>-<repo>.json` at the repository root.
Outside a Git repository, Quorum uses the current directory. Use `--out PATH` to select another file.
The `post-plan` command includes priors from a file less than one week old.
The partition script also accepts `--priors PATH`.

</details>

## Use the cloud runner

The runner normally reads scored clusters from the triage comment.
Use the skill or `quorum synthesize` first. Alternatively, supply a local file with `--scored PATH`.

The runner selects the `agreed` lane by default. Add `--min-quorum 1` to include `solo` clusters.
Use `--cluster ID[,ID]` to select specific clusters.

### Create a local plan

Use an existing `clusters.scored.json` file:

```bash
quorum plan-pr https://github.com/OWNER/REPO/pull/123 \
  --scored clusters.scored.json \
  --plan-only \
  --no-canvas-mirror
```

This command creates the task graph, initial report, and Canvas without cloud or GitHub calls.
Keep the explicit `--plan-only` flag to skip cloud calls in the current runner.
Without `--scored`, Quorum reads the triage comment from GitHub.
Use `--no-canvas-mirror` for offline or CI runs to keep Canvas files in the output directory.

### Run cloud exploration

Save the API key for your provider:

```bash
quorum auth
```

Cursor Cloud is the default provider. The menu also supports Anthropic.

Run exploration without posting a comment:

```bash
quorum run-pr https://github.com/OWNER/REPO/pull/123
```

Run exploration and post its report:

```bash
quorum post-pr https://github.com/OWNER/REPO/pull/123
```

The exploration report uses a separate PR comment. Later runs update that comment.
The runner does not yet record exploration results in the triage comment.

<details>
<summary><strong>Providers and model settings</strong></summary>

Select a provider for one run with `--provider cursor` or `--provider anthropic`.
Set `QUORUM_PROVIDER` to change the default.

| Task complexity | Cursor Cloud | Anthropic |
| --- | --- | --- |
| `HIGH` | `gpt-5.3-codex` | `claude-opus-4-8` |
| `MED` | `composer-2.5` | `claude-sonnet-4-6` |
| `LOW` | `composer-2.5` | `claude-haiku-4-5` |

Override these repository defaults with `QUORUM_MODEL_HIGH`, `QUORUM_MODEL_MED`, or `QUORUM_MODEL_LOW`.
Environment variables take precedence over a saved `dag.json`. The saved graph takes precedence over the defaults.

The Anthropic provider uses the PR diff as code context, with a limit of 200,000 characters.
Use an authenticated `gh` CLI or `GITHUB_TOKEN` to permit access to the diff.
If the diff is unavailable, the provider continues with a missing-context note and reports missing evidence.

</details>

<details>
<summary><strong>Save and manage API keys</strong></summary>

`quorum auth` saves keys and the default provider to `$XDG_CONFIG_HOME/quorum/credentials.json`.
Without `XDG_CONFIG_HOME`, it uses `~/.config/quorum/credentials.json`.
The file has mode `0o600`. Use `QUORUM_CONFIG` to select another path.

API keys use this order, from highest to lowest priority:

1. The `--api-key` flag.
2. The `CURSOR_API_KEY` or `ANTHROPIC_API_KEY` environment variable.
3. The saved credentials file.

The provider uses this order: `--provider`, `QUORUM_PROVIDER`, the saved file, then `cursor`.

```bash
quorum auth --cursor-key "crsr_..."
quorum auth --anthropic-key "sk-ant-..." --provider anthropic
quorum auth --provider anthropic
quorum auth --show
quorum auth --clear
```

`--show` masks stored values. `--clear` removes all stored keys.
The `quorum setup` command checks both environment variables and the saved file.

</details>

<details>
<summary><strong>Advanced commands and options</strong></summary>

Run synthesis and exploration in one command:

```bash
quorum triage-pr https://github.com/OWNER/REPO/pull/123 --min-quorum 1
```

Without a cluster file, synthesis creates separate clusters for each finding.
The example includes `solo` clusters so exploration can select them.

Use the lower-level exploration command with a local file:

```bash
quorum explore \
  --repo OWNER/REPO \
  --pr 123 \
  --scored path/to/clusters.scored.json \
  --no-post
```

| Option | Effect |
| --- | --- |
| `--provider NAME` | Select `cursor` or `anthropic`. |
| `--api-key KEY` | Override the provider key for this run. |
| `--cluster ID[,ID]` | Select specific clusters. |
| `--min-quorum N` | Set the minimum reviewer count. The default is 2. |
| `--max-clusters N` | Limit the number of clusters. |
| `--scored PATH` | Read local input for `plan-pr`, `run-pr`, `post-pr`, or `explore`. |
| `--out DIR` | Select the output directory. |
| `--concurrency N` | Set the task limit. The default is 4 simultaneous tasks. |
| `--task-timeout-ms N` | Set the task timeout. The default is 1,200,000 milliseconds. |
| `--no-post` / `--dry-run` | Suppress comments for `explore` and `triage-pr`. Use `run-pr` for exploration without a comment. |
| `--post` | Post the exploration report after `run-pr`. |
| `--plan-only` | Skip cloud tasks. `triage-pr` still reads GitHub and also needs `--no-post` to suppress the exploration comment. |
| `--no-stream` | Wait for final results from Cursor Cloud. |
| `--canvas-path PATH` | Select the Canvas file path. |
| `--no-canvas` | Omit the Canvas. |
| `--no-canvas-mirror` | Omit the copy in Cursor's managed directory. |

Run a saved task graph:

```bash
quorum run-dag --dag dag.json --out DIR --repo OWNER/REPO
```

Summarize task outcomes and durations from earlier runs:

```bash
quorum eval --log-dir DIR
```

</details>

## Outputs

### The triage comment

The comment starts with a **merge gate**: a review status calculated from the recorded decisions.
It then shows counts, both lanes, and owner deferrals. It preserves links to the original review comments.

| Gate | Meaning |
| --- | --- |
| `⏳ Triage pending` | No cluster has a current disposition. |
| `🛑 Blocked by N issue(s)` | Triage started. A critical or major cluster still needs review or a fix. |
| `✅ Merge gate clear` | Triage started. Every critical or major cluster has a fix, an invalid verdict, or an owner deferral. |

Only critical and major clusters can block the gate. Minor issues and nits remain visible.
A `valid` or `partly` verdict blocks the gate until the ledger records a fixing commit.
The comment also shows the fixing commits, tests, and a timeline from plan to validation to fix.

An expandable section contains the triage protocol and `quorum/triage@1` JSON.
The **ledger** is this structured record inside the comment.
Agents can use it to continue triage without the Quorum skill.

<details>
<summary><strong>Ledger fields and recovery</strong></summary>

The ledger includes:

- The PR, head SHA, totals, reviewer counts, lanes, and available priors.
- Each cluster's quorum, priority tag, source comments, locations, and `mechanism_agreement`.
- Each disposition's verdict, note, evidence, commit, tests, author, and time.
- Each agreed cluster's escalation: the required code rule, similar sites, common fix location, and test.
- The derived `gate`, `deferred`, and `timeline` fields.

A disposition can have `stale: true` when later PR changes affect its evidence.
The gate treats a stale disposition as missing.

The triage comment uses the hidden marker `<!-- quorum:synthesis -->`.
Only `scripts/quorum_ledger.py` writes this comment. Every write rebuilds the comment from its JSON.

If the JSON exceeds GitHub's comment limit, the ledger removes member URLs first, then similar sites.
As a final step, it writes a minimal document with `truncated: true`.
Supply the local file with `--scored clusters.scored.json` when a ledger command needs to rebuild a truncated document.

To use the scripts without an installed skill, fetch the tagged archive:

```bash
mkdir -p quorum
gh api repos/Wenjix/quorum/tarball/v0.3.0 > quorum.tgz
tar -xzf quorum.tgz -C quorum --strip-components=1
python3 quorum/scripts/quorum_ledger.py show OWNER/REPO PR
```

The archive contains a `Wenjix-quorum-<sha>/` directory. The extraction command removes that directory level.
Keep `quorum/scripts/` and `quorum/references/` together.

</details>

### Exploration files

Each shortcut run writes to `.quorum/runs/<repo>-pr<N>-<timestamp>/` by default.

| File | Contents |
| --- | --- |
| `input.clusters.scored.json` | Scored input clusters |
| `dag.json` | The directed acyclic graph (DAG) that defines tasks and dependencies |
| `state.json` | Task status, models, run and agent IDs, errors, and results |
| `exploration.json` | Structured root causes and similar bug patterns |
| `exploration.md` | The report for people |
| `quorum-exploration.canvas.tsx` | The Cursor Canvas with task status and summaries |

The exploration comment uses the hidden marker `<!-- quorum:exploration -->`.
Later runs update the existing comment when you enable posting.

### Cursor Canvas

The Canvas shows task status, agent links, durations, and results.
Quorum updates it when a task starts, finishes, fails, or skips execution.

Cursor renders interactive Canvas files from its managed directory:

```text
~/.cursor/projects/<workspace-slug>/canvases/
```

Quorum keeps a copy there as `quorum-<repo>-pr<N>.canvas.tsx` and prints the path as `canvas (Cursor)`.
Open that path in Cursor to view the Canvas beside the chat.

Quorum omits this copy if Cursor has no managed directory for the workspace.
The flags `--no-canvas-mirror` and `--no-canvas` also prevent this copy.

Rebuild a Canvas from saved results without another exploration run:

```bash
quorum canvas .quorum/runs/RUN_ID
```

Replace `RUN_ID` with the saved run directory name.

## Safety defaults

- **Conservative groups.** Keep findings separate when the evidence for a shared cause is uncertain.
- **Bot comments only.** Accept accounts with type `Bot` whose login matches the reviewer filter.
  The default filter is `cursor\[bot\]|copilot|devin|greptile|coderabbit`. Use `QUORUM_BOTS` to change it. Human accounts never qualify.
- **Trusted ledgers.** Accept comments from the authenticated `gh` login or an account with `OWNER`, `MEMBER`, or `COLLABORATOR` association.
- **Owner-controlled fixes.** Triage agents propose fixes. The owner decides whether to apply them.
- **Read-only exploration.** Both providers use read-only prompts and configuration.
  Cursor uses `autoCreatePR: false`. Anthropic receives the PR diff and instructions against edits, commits, or pushes.

**Hosted automation has a limitation.** GitHub App and Actions tokens cannot use `GET /user` for the ledger trust check.
A bot comment with association `NONE` also fails that check. These runs can create duplicate comments.

## Development

| Command | Purpose |
| --- | --- |
| `npm run typecheck` | Check TypeScript types. |
| `npm test` | Build, then run the Node and Python test suites. |
| `npm run build` | Build the CLI. |
| `npm run build:skill` | Package the skill from the latest commit. |
| `node dist/src/cli.js --help` | Show CLI commands and options after a build. |

After changes to `SKILL.md`, `scripts/`, or `references/`, commit those changes first.
Then run `npm run build:skill`. Commit the refreshed `quorum.skill` file.
The package command reads `HEAD` and excludes uncommitted changes.

<details>
<summary><strong>Repository guide</strong></summary>

| Path | Purpose |
| --- | --- |
| [SKILL.md](SKILL.md) | Skill instructions |
| [quorum.skill](quorum.skill) | Installable skill package without tests |
| [scripts/](scripts/) | Python 3.9-compatible scripts for findings, groups, ledgers, and priors |
| [scripts/tests/](scripts/tests/) | Python tests, excluded from skill and npm packages |
| [references/](references/) | Clustering rules, triage protocol, and agent prompts |
| [src/](src/) | TypeScript CLI and cloud runner |
| [test/](test/) | Node tests |
| [docs/](docs/) | Design and dependency notes |

The main scripts are [quorum_ledger.py](scripts/quorum_ledger.py), [validate_partition.py](scripts/validate_partition.py), and [quorum_priors.py](scripts/quorum_priors.py).

The CLI starts in [src/cli.ts](src/cli.ts).
The modules [synthesis.ts](src/synthesis.ts), [clusters.ts](src/clusters.ts), and [dag.ts](src/dag.ts) recover input, select clusters, and define tasks.
The modules [runner.ts](src/runner.ts), [report.ts](src/report.ts), and [canvas.ts](src/canvas.ts) execute tasks and produce outputs.
The provider adapters are [cursor-cloud-adapter.ts](src/cursor-cloud-adapter.ts) and [adapters/anthropic.ts](src/adapters/anthropic.ts).
The modules [config.ts](src/config.ts) and [github.ts](src/github.ts) manage credentials and GitHub access.

See [AGENTS.md](AGENTS.md) for contributor instructions and [docs/dependencies.md](docs/dependencies.md) for dependency decisions.

</details>

## Current status

Quorum supports interactive use during its own development. It is not a hosted GitHub App.
See the [triage design](docs/superpowers/specs/2026-10-28-quorum-triage-director-design.md) for the current approach.

Planned work:

- Record cloud exploration results in the triage ledger.
- Support configured trusted logins for GitHub App and Actions tokens.
- Record owner decisions from replies in review threads.
- Count reviewers that complete a review without findings.
- Use run logs and priors to evaluate false positives and merges with missed bugs.
- Add hosted automation through GitHub Actions or a GitHub App.
