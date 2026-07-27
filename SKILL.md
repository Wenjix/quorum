---
name: quorum
description: Synthesize, deduplicate, prioritize, and explore automated code-review findings on GitHub pull requests. Use whenever the user mentions Bugbot, Copilot code review, Devin reviewer, CodeRabbit or other review bots, bot review triage, duplicate or noisy review comments, reviewer consensus or quorum, Quorum DAG exploration, Cursor Cloud root-cause analysis, pattern sweeps, or asks to summarize, dedupe, cluster, rank, synthesize, or explore automated PR feedback — even if they do not name a specific tool. Fetches bot review comments via gh, clusters same-root-cause findings, computes reviewer quorum deterministically, posts one idempotent synthesis comment with machine-readable JSON, and can launch the Quorum Cloud DAG runner for root-cause and pattern-sweep follow-up.
---

# Quorum — bot review synthesis

Multiple review bots (Bugbot, Copilot, Devin, …) comment on the same PR with overlapping findings. This skill turns that noise into one prioritized, consensus-scored synthesis.

Pipeline: **fetch (script) → cluster (you) → validate & score (script) → post (script)**.

Core design rules — do not violate these:

1. **You are the clustering judge, but quorum is computed in code.** Never compute or state quorum counts yourself; `validate_partition.py` derives it from distinct reviewers per cluster.
2. **Precision over recall on merges.** A false merge fabricates reviewer consensus, which poisons the product's core signal. When uncertain, don't merge.
3. **You match; you do not re-review.** Do not judge whether findings are valid, and do not drop findings you disagree with. Every finding lands in exactly one cluster.
4. **Clustering failure never blocks the synthesis.** Fallback path is all-singletons.

## Preconditions

- `gh` CLI authenticated with access to the repo (`gh auth status`), plus `jq` and `python3`.
- A target PR. Use the number/URL the user gave; otherwise resolve from the current branch with `gh pr view --json number,headRepository,url`. If ambiguous, ask.

Work in a scratch directory (e.g. `/tmp/quorum-<pr>/`) so intermediate JSON doesn't pollute the repo.

`scripts/` and `references/` live alongside this SKILL.md. Resolve those paths relative to the skill's own directory (e.g. `.claude/skills/quorum/scripts/fetch_findings.sh`) — your working directory is usually the repo under review, where those paths don't exist.

## Step 1 — Fetch findings

```bash
scripts/fetch_findings.sh OWNER/REPO PR_NUMBER findings.json
```

Produces normalized records: `{id, reviewer, login, file, lines, outdated, body, hunk, url, comment_id, node_id, reply_count, human_reply_count, reactions}`. IDs look like `bugbot-1`, `devin-2` and are assigned in comment-creation order, so a re-run after new bot comments appends new ids instead of reshuffling existing ones.

- The bot allow-list is a case-insensitive regex on the comment author login, default `cursor\[bot\]|copilot|devin`, overridable via the `QUORUM_BOTS` env var. Accounts matching the regex but not of GitHub type `Bot` are skipped (a human named e.g. `devinsmith` must not inflate the reviewer pool); if the script's stderr notes a skipped account that really is a review bot posting from a user account, re-run with `QUORUM_ALLOW_HUMANS=1`.
- **If 0 findings match** but the script's stderr lists authors that are clearly review bots, set `QUORUM_BOTS` to match those logins and re-run. Do not proceed on an empty set — instead tell the user what was found and which expected reviewers haven't commented yet (bots finish at different times; suggest re-running after all have reported).
- Findings are line-anchored review comments only; top-level bot summary comments are deliberately ignored in v0. Thread replies are folded into the parent finding as `reply_count` / `human_reply_count`; those and `reactions` are validity signal for prioritization and the eval flywheel — they never affect clustering or quorum.

## Step 2 — Cluster (you are the judge)

Read `references/clustering-rubric.md` **in full** before clustering — it defines the single-fix test, merge/don't-merge rules, the output schema, and worked examples.

Then write `clusters.json` yourself, following the rubric exactly. Reminders:

- Strict JSON, exact schema, every finding ID in exactly one cluster.
- Singletons are normal. Most clusters will be singletons.
- Do not include quorum. Do not omit findings you think are false positives.
- Every multi-finding cluster needs `single_fix`: one sentence naming the concrete change that resolves every member. If you cannot write that sentence, the merge fails the single-fix test — keep the findings separate.

**Large PRs (more than ~30 findings):** don't judge all pairs in one pass — attention spreads across O(n²) implicit comparisons and merge precision drops. Instead run:

```bash
python3 scripts/propose_candidates.py findings.json -o candidates.json
```

Judge only the candidate pairs it proposes (same-file proximity and body-similarity blocking); findings absent from every candidate pair default to singletons. A listed pair is *not* presumed to match — apply the single-fix test to each.

**Borderline merges** (you'd put confidence in 0.7–0.85): re-derive the merge in a second, independent look at just those findings before keeping it. Consensus that doesn't reproduce isn't consensus — drop the merge.

## Step 3 — Validate & score

```bash
python3 scripts/validate_partition.py findings.json clusters.json -o clusters.scored.json
```

The script enforces the partition invariant and schema, applies the confidence gate (multi-finding clusters with `match_confidence < 0.7` are split back into singletons), computes `quorum` per cluster, sorts by (quorum desc, severity desc), and writes `clusters.scored.json`.

- **Exit ≠ 0:** the violations are named on stderr. Fix `clusters.json` and re-run.
- **After two failed attempts:** regenerate `clusters.json` as all singletons (one cluster per finding, `match_type: "singleton"`, `match_confidence: 1.0`) and proceed. Never block on clustering.

## Step 4 — Post

```bash
python3 scripts/post_synthesis.py OWNER/REPO PR_NUMBER clusters.scored.json --dry-run
```

`--dry-run` prints the rendered comment without touching GitHub. In an interactive session, show the user the rendered synthesis (or a tight summary of it) before posting, unless they already said to post/comment. When running non-interactively (e.g. claude-code-action), post directly.

To post for real, drop `--dry-run`. The script:

- **Upserts** a single synthesis comment, idempotent via a hidden `<!-- quorum:synthesis -->` marker — re-runs update in place, never spam — then fetches the comment back and verifies the marker landed.
- Adds 👀 reactions to the original bot comments belonging to quorum ≥ 2 clusters (the in-thread visual cue that another reviewer agrees).
- Embeds the scored clusters JSON in a collapsed `<details>` block — the machine-readable surface for downstream agents. Oversized PRs degrade the embed gradually (pretty → compact → slimmed → omitted) instead of dropping it outright.
- `--minimize` additionally collapses non-primary duplicate comments as DUPLICATE. This hides other bots' comments, so treat it as **opt-in: only with explicit user consent**.

## Step 5 — Report and offer follow-ups

Summarize in conversation: findings → clusters, quorum tiers, the top finding. Then offer the natural next actions:

1. **Fix now** — implement fixes for quorum ≥ 2 clusters in this session.
2. **Cloud DAG exploration** — run Cursor Cloud root-cause and pattern-sweep agents with `quorum run-pr PR_URL` (or `quorum plan-pr PR_URL` first for a no-cloud dry plan).
3. **Log the run** for the eval flywheel (below).

## Phase 2 — Cloud DAG shortcuts

If the repo has this Quorum CLI built and linked, prefer the shortcut commands over hand-writing the long `explore --repo --pr --scored` form.

```bash
quorum plan-pr https://github.com/OWNER/REPO/pull/N
quorum run-pr https://github.com/OWNER/REPO/pull/N
quorum post-pr https://github.com/OWNER/REPO/pull/N
quorum canvas .quorum/runs/<run-id>
```

- `plan-pr` recovers the embedded `clusters.scored.json` from the existing `<!-- quorum:synthesis -->` PR comment, writes the DAG/report/Canvas, and does not call Cursor Cloud.
- `run-pr` runs the Cursor Cloud DAG and writes local artifacts/Canvas, but does not post an exploration comment by default.
- `post-pr` runs the DAG and upserts the PR exploration comment.
- Add `--scored path/to/clusters.scored.json` if no synthesis comment exists yet or the user has a local scored file.
- Add `--min-quorum 1` when testing on PRs where all findings are single-reviewer findings.
- Run `quorum canvas .quorum/runs/<run-id>` to regenerate/open the Canvas for an existing run without rerunning cloud agents.

If `quorum` is not on PATH but this repo is checked out, use `node dist/src/cli.js` in place of `quorum` from the Quorum repo root. If the command says no synthesis comment was found, run Steps 1-4 first or pass `--scored`.

## Dogfood logging (recommended)

```bash
scripts/log_run.sh OWNER/REPO PR_NUMBER findings.json clusters.scored.json
```

Snapshots the run to `.quorum/log/<date>-pr<N>.json` (gitignored) with an `outcomes` stub per cluster. When the PR settles, the user labels each cluster (`fixed` | `dismissed` | `false-positive`, plus `bad_merge: true` on any multi-finding cluster that merged distinct issues). Then:

- `python3 scripts/reviewer_reliability.py` turns labeled logs into per-bot precision, per-category precision, inter-bot error correlation, and merge-confidence calibration — the data that should set the validator's `--confidence-gate` and, eventually, weight quorum by reviewer reliability.
- Every `bad_merge` becomes a new worked example in the rubric and a new gold fixture under `evals/` (see `evals/README.md` for the clustering eval harness: merge precision/recall against gold partitions, calibration, and order-stability checks). The skill improves from exactly the failures that matter.

## Known v0 simplifications

- Quorum denominator = reviewers with ≥ 1 finding on this PR. A bot that reviewed and found nothing isn't counted (its implicit "looks fine" is real signal, but capturing it needs review-event data — later).
- Force-pushes can orphan comment anchors (`outdated: true` in records). Re-run Step 1 after a push.
- Two bots sharing a false positive still count as quorum 2 — consensus is signal, not truth. The log exists to measure exactly this.
