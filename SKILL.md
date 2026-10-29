---
name: quorum
description: Triage director for automated code-review findings on GitHub pull requests. Use whenever the user mentions Bugbot, Copilot code review, Devin reviewer, CodeRabbit, Greptile or other review bots, bot review triage, duplicate or noisy review comments, reviewer consensus or quorum, Quorum DAG exploration, Cursor Cloud root-cause analysis, pattern sweeps, or asks to summarize, dedupe, cluster, rank, synthesize, triage, or explore automated PR feedback — even if they do not name a specific tool. Fetches bot review comments via gh, clusters same-root-cause findings, computes reviewer quorum deterministically, sorts clusters into an agreed lane (escalate) and a solo lane (prove or kill), keeps one ledger comment per PR with a merge gate and machine-readable JSON, drives read-only escalation and validator subagents, and can launch the Quorum Cloud DAG runner.
---

# Quorum — bot review triage director

Multiple review bots (Bugbot, Copilot, Devin, …) comment on the same PR with overlapping findings. This skill turns that noise into a triage plan with two lanes, drives the triage, and keeps the outcome in one ledger comment on the PR.

Pipeline: **fetch (script) → cluster (you) → validate & score (script) → post the plan (script) → triage the lanes (subagents) → record dispositions (script)**.

The lane is derived in code from quorum: a cluster raised by two or more distinct reviewers is `agreed`, a cluster raised by one is `solo`.

Core design rules — do not violate these:

1. **You are the clustering judge, but quorum is computed in code.** Never compute or state quorum counts yourself; `validate_partition.py` derives it from distinct reviewers per cluster, and the lane from quorum.
2. **Precision over recall on merges.** A false merge fabricates reviewer consensus, which poisons the product's core signal and lifts the cluster into the `agreed` lane, where it is presumed real. When uncertain, don't merge.
3. **While clustering, you match; you do not re-review.** Do not judge whether findings are valid, and do not drop findings you disagree with. Every finding lands in exactly one cluster. Validity is settled per lane in Step 4 and recorded in the ledger.
4. **Clustering failure never blocks the synthesis.** Fallback path is all-singletons.
5. **Every comment write goes through `scripts/quorum_ledger.py`.** Each write re-renders the whole comment from its embedded JSON, so a hand edit is lost on the next write; change what the comment says by recording a `dispose` or `escalate`.
6. **Agreed clusters are presumed real: escalate, never re-prove.** Two or more independent reviewers already found the defect; spend the work on the missing invariant, its sibling sites and the fix. An escalation returns `partly` or `invalid` only with evidence that disproves the premise.

## Preconditions

- `gh` CLI authenticated with access to the repo (`gh auth status`), plus `jq` and `python3`.
- A target PR. Use the number/URL the user gave; otherwise resolve from the current branch with `gh pr view --json number,headRepository,url`. If ambiguous, ask.

Work in a scratch directory (e.g. `/tmp/quorum-<pr>/`) so intermediate JSON doesn't pollute the repo. The `scripts/` and `references/` paths below are relative to this skill's directory.

## Step 0 — Fetch findings and count reviewers

```bash
scripts/fetch_findings.sh OWNER/REPO PR_NUMBER findings.json
```

Produces normalized records: `{id, reviewer, login, file, lines, outdated, body, hunk, url, comment_id, node_id, pr, head, stated_severity}`. IDs look like `bugbot-1`, `devin-2`. stderr lists every author seen, then one line per matched reviewer with its finding count.

- Only comments from Bot-type accounts are ingested, and only when the login matches a case-insensitive regex: default `cursor\[bot\]|copilot|devin|greptile|coderabbit`, overridable via the `QUORUM_BOTS` env var. A human account is never ingested, whatever its login.
- **If 0 findings match** but stderr lists bot logins (usually ending in `[bot]`) that the regex misses, set `QUORUM_BOTS` to match them and re-run; a reviewer posting from a user account cannot be ingested at all. Do not proceed on an empty set — instead tell the user what was found and which expected reviewers haven't commented yet (bots finish at different times; suggest re-running after all have reported).
- **If 1 distinct reviewer matched** (`jq '[.[].reviewer] | unique | length' findings.json` prints `1`), skip Step 1 and the rubric entirely and go to Step 2's `--all-singletons` form. Every cluster lands in `solo`.
- Findings are line-anchored review comments only; top-level bot summary comments are deliberately ignored.

## Step 1 — Cluster (you are the judge)

Read `references/clustering-rubric.md` **in full** before clustering — it defines the single-fix test, merge/don't-merge rules, the output schema, and worked examples.

Then write `clusters.json` yourself, following the rubric exactly. Reminders:

- Strict JSON, exact schema, every finding ID in exactly one cluster.
- Singletons are normal. Most clusters will be singletons.
- Every cluster of size > 1 carries `mechanisms_claimed` (one entry per member, in that reviewer's own terms) and `mechanism_agreement` (`compatible | split | unknown`). You compare what the reviewers said; you never decide which one is right.
- Do not include quorum. Do not omit findings you think are false positives.

## Step 2 — Validate & score

```bash
python3 scripts/validate_partition.py findings.json clusters.json -o clusters.scored.json \
  [--priors <repo-root>/.quorum/priors/<owner>-<repo>.json]

# --all-singletons form (no clusters.json): one reviewer, or the fallback below
python3 scripts/validate_partition.py findings.json --all-singletons -o clusters.scored.json \
  [--priors <repo-root>/.quorum/priors/<owner>-<repo>.json]
```

Pass `--priors` when that file exists; it holds each reviewer's past precision in this repo and is embedded as `priors`. The cache lives under the repository's `.quorum/priors/` (its git top level, `git rev-parse --show-toplevel`), not in the scratch directory.

The script enforces the partition invariant and schema, applies the confidence gate (multi-finding clusters with `match_confidence < 0.7` are split back into singletons), computes `quorum` and `lane` per cluster, sorts by (quorum desc, severity desc), and writes `clusters.scored.json` as a `quorum/triage@1` document: the PR and its head, per-reviewer counts, `lanes` (the `agreed` and `solo` cluster ids), and per cluster its lane, P-tag, validation mode and the union of every member's locations.

- **Exit ≠ 0:** the violations are named on stderr. Fix `clusters.json` and re-run.
- **After two failed attempts:** run the `--all-singletons` form (in place of `clusters.json`) and proceed. Never block on clustering.

## Step 3 — Post the plan

```bash
python3 scripts/quorum_ledger.py post-plan OWNER/REPO PR_NUMBER clusters.scored.json --dry-run
```

`--dry-run` prints the rendered comment to stdout and the planned side effects to stderr, and writes nothing to GitHub. In an interactive session, show the user the rendered plan (or a tight summary of it) before posting, unless they already said to post/comment. When running non-interactively (e.g. claude-code-action), post directly. When the owner wants nothing posted (`--no-comment`: a choice in this skill, not a script flag; typical on single-reviewer PRs), skip this step. With no ledger, `show`, `escalate` and `dispose` have nothing to read or write; Steps 4 to 6 say what to do instead.

To post for real, drop `--dry-run`. The script:

- **Upserts** one comment, found by its hidden `<!-- quorum:synthesis -->` marker and trusted only when written by your account or a repo owner, member or collaborator. Re-runs update it in place, never spam.
- Renders a human brief (merge gate line, the `agreed` and `solo` lanes, owner-deferred clusters) above a collapsed agent layer: the protocol from `references/triage-protocol.md` and the `quorum/triage@1` JSON.
- Carries dispositions forward, so re-running Steps 0–3 after a push is safe: a new cluster inherits the latest disposition of any prior cluster sharing a member comment, and one whose files changed since is marked `stale: true` (shown as re-check), unless the push carries the disposition's own `--commit` fix.
- Adds 👀 reactions to the bot comments in `agreed` clusters; `--no-reactions` skips them.
- `--minimize` additionally collapses non-primary duplicate comments as DUPLICATE. This hides other bots' comments, so treat it as **opt-in: only with explicit user consent**.

## Step 4 — Offer to drive the triage

Offer to work the lanes with subagents, unless the owner already asked for triage; a non-interactive run drives it only when the request asked for triage. The lanes mean what the protocol in `references/triage-protocol.md` says:

> 1. `agreed` clusters: do not re-prove. Establish the missing invariant, sweep for sibling sites, write a fix brief naming the chokepoint and the test. Report with `escalate`.
> 2. `solo` clusters: prove or kill. Cheapest reproduction first. Return `valid | partly | invalid` with evidence. Report with `dispose`. `priors` in the JSON is each reviewer's past precision here: a prior, never a verdict.

1. **Check the checkout is the PR head.** Subagents read local files. If `git rev-parse HEAD` differs from `head` in `clusters.scored.json`, offer `gh pr checkout PR_NUMBER` before dispatching.
2. **Pick the open clusters.** `python3 scripts/quorum_ledger.py show OWNER/REPO PR_NUMBER > triage.json` prints the posted ledger. Skip clusters that already carry a `disposition` unless it is marked `stale: true`. If `triage.json` has `truncated: true`, add `--scored clusters.scored.json` to every `escalate` and `dispose` call below. With no ledger (`--no-comment`), every cluster in `clusters.scored.json` is open.
3. **Dispatch subagents that make no tracked-file edits, all in parallel** (they may run tests and throwaway commands):
   - one escalation subagent per open `agreed` cluster, prompted with `references/escalation-prompt.md`;
   - one validator subagent per batch of up to four open `solo` clusters, prompted with `references/validator-prompt.md`. Batch by file (`primary_location.file`), so one validator reads related code.

   Each prompt file names the slots to fill; the cluster objects come from `clusters.scored.json`.
4. **Save each result as a file** in the scratch directory: an escalation's `QUORUM_ESCALATION` block as `escalation-<cluster_id>.json`; a validator batch's `QUORUM_DISPOSITION` blocks together as `{"dispositions": [...]}` in `dispositions-<n>.json`. A cluster whose subagent returned no parseable block stays pending; say so in the report.
5. **Record each file in the ledger:**

   ```bash
   python3 scripts/quorum_ledger.py escalate OWNER/REPO PR_NUMBER CLUSTER_ID --from escalation-<cluster_id>.json
   python3 scripts/quorum_ledger.py dispose OWNER/REPO PR_NUMBER --from dispositions-<n>.json --reply
   ```

   `escalate` stores the escalation and a disposition from its verdict in one update; `dispose --reply` also replies in each member's thread. A refusal names the offending field and writes nothing: fix the file and re-run. With no ledger, skip this step and give the results in conversation.

Step 4 is done when every open cluster has a recorded disposition (with no ledger, a reported result) or is reported as pending. Summarize the verdicts per lane, the merge gate, and each escalation's fix brief: invariant, likely siblings, chokepoint, test. `show` always prints the current `gate` (`state`, `blocking`), truncated ledger or not.

## Step 5 — Offer fixes

Fixes are the owner's call: the fix brief is a proposal until the owner says go, or said "triage and fix" up front. Offer fixes for clusters with a `valid` or `partly` verdict, starting with those holding the merge gate (`gate.blocking` in a fresh `show`; with no ledger, the critical and major ones). For an `agreed` cluster, fix at the escalation's chokepoint, cover its likely siblings, and add its test.

After each fix is committed, record it per fixed cluster (with no ledger, skip this call):

```bash
python3 scripts/quorum_ledger.py dispose OWNER/REPO PR_NUMBER CLUSTER_ID --commit SHA --tests "what ran and passed" --reply
```

A call carrying only `--commit` and `--tests` keeps the prior verdict and note; a cluster with no disposition yet also needs `--verdict` and `--note`. `SHA` is 7 to 40 lowercase hex characters. When the owner declines a fix, record their call with `--verdict deferred --note "<their reason>" --by owner`; deferred clusters move to their own section and stop holding the gate.

## Step 6 — Report and log

Summarize in conversation: findings → clusters, the two lanes, the gate state, what the triage found, and what is still pending. Offer Cloud DAG exploration (Phase 2) when the user wants a cloud run.

Log the run for the eval flywheel. Append `{pr, findings, clusters_scored, triage}` to `.quorum/log/<date>-pr<N>.json` (gitignored in the Quorum repo) or a path the user prefers, where `triage` is the output of `show`:

```bash
python3 scripts/quorum_ledger.py show OWNER/REPO PR_NUMBER > triage.json
mkdir -p .quorum/log
jq -nc --arg pr "OWNER/REPO#PR_NUMBER" --slurpfile f findings.json \
  --slurpfile s clusters.scored.json --slurpfile t triage.json \
  '{pr: $pr, findings: $f[0], clusters_scored: $s[0], triage: $t[0]}' \
  >> ".quorum/log/$(date +%F)-pr<N>.json"
```

With no posted ledger, `triage` comes out `null`. Every bad merge the user spots becomes a new worked example in the rubric — the skill improves from exactly the failures that matter.

## Phase 2 — Cloud DAG shortcuts

If the repo has this Quorum CLI built and linked, prefer the shortcut commands over hand-writing the long `explore --repo --pr --scored` form.

```bash
quorum plan-pr https://github.com/OWNER/REPO/pull/N
quorum run-pr https://github.com/OWNER/REPO/pull/N
quorum post-pr https://github.com/OWNER/REPO/pull/N
quorum canvas .quorum/runs/<run-id>
```

- `plan-pr` recovers the embedded `quorum/triage@1` JSON from the `<!-- quorum:synthesis -->` ledger comment, writes the DAG/report/Canvas, and does not call Cursor Cloud.
- `run-pr` runs the Cursor Cloud DAG and writes local artifacts/Canvas, but does not post an exploration comment by default.
- `post-pr` runs the DAG and upserts its own `<!-- quorum:exploration -->` comment. Until the runner follow-up writes escalations into the ledger, it is the one path that adds a second comment.
- The commands select the `agreed` lane by default. `--min-quorum N` overrides that with a quorum floor (`--min-quorum 1` takes the solo lane too); `--cluster ID[,ID]` picks clusters by id.
- Add `--scored path/to/clusters.scored.json` if no ledger comment exists yet or the user has a local scored file.
- Run `quorum canvas .quorum/runs/<run-id>` to regenerate/open the Canvas for an existing run without rerunning cloud agents.

If `quorum` is not on PATH but this repo is checked out, use `node dist/src/cli.js` in place of `quorum` from the Quorum repo root. If the command says no synthesis comment was found, run Steps 0–3 first or pass `--scored`.

## Known simplifications

- Quorum denominator = reviewers with ≥ 1 finding on this PR. A bot that reviewed and found nothing isn't counted (its implicit "looks fine" is real signal, but capturing it needs review-event data — later).
- An agent without this skill (Devin CLI, Cursor, …) can still triage: the protocol embedded in the comment's collapsed agent layer tells it what each lane needs and how to report through `quorum_ledger.py`.
