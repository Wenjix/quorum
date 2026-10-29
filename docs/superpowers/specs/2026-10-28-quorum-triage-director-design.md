# Quorum as triage director — design

Date: 2026-10-28
Status: draft for owner approval
Owner: Wenjie (Wenjix)
Written by: Claude, from a brainstorming conversation on 2026-10-28

## 1. Intent

Several AI reviewers (Cursor Bugbot, Devin Review, Greptile, Copilot,
CodeRabbit) comment on the same pull request. Today Quorum deduplicates
those comments into clusters, computes how many distinct reviewers agree
on each cluster, and posts one synthesis comment. A triage agent (Claude
Code, Devin CLI, or Cursor) then validates every cluster with the same
effort, fixes what is real, and replies in each thread.

The synthesis costs tokens and time before validation starts, and its
output does not change what the triage agent does next. The owner wants
that cost to be load-bearing:

> "if we have multiple bots reviewing one PR, and there's some common
> overlap and then there's some disagreement, that potentially helps us
> direct what is more important to validate versus what is likely a real
> issue seen by multiple bots. And then ... if there is an issue that so
> clearly was spotted by multiple bots, what else is worth investigating?"

> "a junior engineer can just go through the comments ... But if a staff
> engineer or a principal engineer looked at PRs, often they might have
> intuition or be able to discover a more elegant fix."

> "I would want to be able to quickly ... present the information and
> findings in a way that a human can understand. I also want information
> that can help agents who actually do the triage perform better. Ideally
> ... surface both, one for myself and one for agents, in one comment."

### Evidence from the last four real sessions

| PR | Findings | Clusters | Agreed clusters | Validation outcome on agreed clusters |
|---|---|---|---|---|
| DejaLango-next #4/#6 | 5 | 4 | 1 | both bots right |
| Odysseus #3 | 7 | 4 | 2 | "valid, and worse than reported" |
| Glyphfield #2/#4 | 3 | skipped | 0 | owner: "You can skip the quorum." |
| DejaLango-next #8 | 14 | 12 | 2 | agent's first ruling was wrong; validator found both bots right |

Three observations drive the design:

1. Dedupe is small. A handful of merges per PR; most clusters are
   singletons.
2. Agreement predicted validity every time, and twice the agreed finding
   was worse than any single bot said. Solo findings were a mix of valid,
   partly valid, wrong mechanism, and by design.
3. Quorum directed nothing. Every session validated every cluster the
   same way. The synthesis comment was posted at the end, after fixes.

## 2. Goals

1. **The comment changes what the triage agent does next.** Each cluster
   is assigned a lane, and the lane names the validation mode.
2. **Agreed clusters get staff-engineer treatment.** Not re-proved.
   Instead: the missing invariant, the sibling sites where it is also
   missing, and a fix brief naming the one change that covers them.
3. **One comment, two readers.** A human brief on top, an agent layer
   collapsed beneath, upserted in place as the triage progresses: plan,
   then validated, then fixed.
4. **Portable.** An agent that does not have the Quorum skill installed
   can follow the protocol from the comment alone. The owner runs triage
   under Claude Code and Devin CLI; both must work.
5. **The ledger creates the dataset.** Dispositions recorded per cluster
   per reviewer accumulate into per-bot precision by lane.
6. **Cost is proportional to signal.** Single-reviewer PRs skip
   clustering. Multi-reviewer clustering cost is unchanged but now
   load-bearing. The new expensive work, escalation, is bounded to agreed
   clusters, which have been one or two per PR.

## 3. Non-goals

- Not a hosted GitHub App. Still interactive and CLI.
- Not re-reviewing during clustering. The clustering judge still matches
  and never judges validity. Validity comes from the validation step and
  is recorded as a disposition.
- Not auto-fixing. The fix brief is a proposal. Making the fix remains the
  owner's call, exactly as today.
- Not minimizing or hiding other bots' comments by default. `--minimize`
  stays opt-in.
- Not a diagram of what the PR changes. Greptile already draws one.
- Not a numeric confidence score.
- Not parsing human free-text replies in bot threads into dispositions.
  That is a later addition; see section 10.
- Not shipping per-bot priors with real numbers on day one. The schema
  slot ships; the data accumulates from ledgers.

## 4. Anti-goals

These would make the work a failure even if it technically runs.

- **Fabricated consensus.** A false merge inflates quorum. The existing
  precision-over-recall rule and the confidence gate stay as they are.
- **A ledger that lies.** A disposition without a verdict and a note is
  invalid. A "fixed" disposition without a commit is invalid.
- **An agent layer only Claude Code can use.** The protocol and JSON must
  be self-describing.
- **A human brief longer than the bot comments it summarizes.** Gate
  line, counts, lane rows. Detail lives in the agent layer and the
  original threads.
- **More than one Quorum comment per PR.** Plan, ledger, and escalation
  results all live in the one upserted comment. This binds the skill
  path in this round; the cloud runner's separate
  `<!-- quorum:exploration -->` comment is retired in the follow-up
  described in 6.13.
- **Escalation that re-proves agreed findings** instead of going to the
  class of defect.
- **Silently losing dispositions** when the PR head moves and clusters
  are recomputed.

## 5. Constraints

- Existing invariants hold: quorum is computed in code, never by the
  model; clustering failure never blocks (fallback is all singletons);
  every comment write is idempotent via a hidden marker.
- New invariant: **every comment write goes through one script, and the
  rendered markdown is a deterministic function of the embedded JSON.**
  Agents never hand-edit the comment.
- Skill scripts stay in bash and python3 with `gh` and `jq`, so the
  packaged `quorum.skill` keeps working in Claude Code and Cursor without
  the Node CLI. The Node CLI may wrap them but must not be required.
- GitHub issue comments are capped at 65,536 characters. The agent layer
  is larger than today's embedded JSON. The builder chooses a size
  strategy (section 11); the protocol, lanes, and per-cluster essentials
  must always fit in the comment.
- The CLI recovers the scored clusters from the PR comment today
  (`recoverScoredClustersFromPullRequest` in `src/synthesis.ts`, which
  calls `parseScoredClusters` in `src/clusters.ts`). The new triage JSON
  must be a **superset** of the current `clusters.scored.json` shape so
  recovery keeps working, and the recovery code must accept the new
  schema id.
- The `<!-- quorum:synthesis -->` marker is kept for backward
  compatibility. Comments written by the old skill version are read, and
  the next write upgrades them in place.
- GitHub renders fenced `mermaid` blocks in comments. The invariant map
  uses one; it must stay small (about twelve nodes) and fall back to a
  bullet list beyond that.

## 6. The design

### 6.1 Pipeline

```
fetch findings
   │
   ├─ one reviewer? ──► no clustering; every finding is a solo cluster
   │
   ▼
cluster (LLM judge, existing rubric + two new fields)
   ▼
validate & score (script): partition check, confidence gate, quorum,
                           lane, locations union, P-tag
   ▼
render & post PLAN (script): human brief + agent layer, one comment
   ▼
triage agent works the lanes          ◄── Claude Code skill offers to
   (reads the agent layer)                drive this with subagents
   ▼
post_disposition (script) per cluster: verdict, note, evidence, commit
   ▼
render & post LEDGER (same comment, upserted)
```

### 6.2 Lanes

Lane is derived in code from quorum.

| Lane | Rule | Validation mode | What the agent does |
|---|---|---|---|
| `agreed` | quorum ≥ 2 | `escalate` | Presume real. Do not re-prove. Establish the missing invariant, sweep the repo for sibling sites, write a fix brief: invariant, siblings with risk, chokepoint, test to add. |
| `solo` | quorum = 1 | `prove_or_kill` | Cheapest reproduction or refutation first. Return `valid`, `partly`, or `invalid` with evidence. |

**Agreed clusters still get a verdict.** Establishing the missing
invariant requires reading the code, so the escalation result carries a
`verdict` that defaults to `valid`. The escalation agent may return
`partly` or `invalid` only when the invariant work disproves the premise,
with evidence; two reviewers sharing a false positive is a known failure
mode and this is where it is caught. The disposition for an agreed
cluster is written from the escalation result. No separate validator
runs on agreed clusters.

One flag, not a third lane: `mechanism_agreement` on every cluster with
more than one member, values `compatible`, `split`, or `unknown`. The
clustering judge sets it by comparing the members' stated mechanisms to
each other. This is matching, not re-reviewing: the judge says whether
the members describe the same mechanism, not whether either is right.
When `split`, the protocol tells the agent to establish the true
mechanism before anyone fixes anything. Evidence this matters: Devin's
EAGAIN finding on DejaLango-next #8 was "mechanism wrong, symptom right".

### 6.3 Clustering rubric additions

Two fields are added to each cluster the judge writes; everything else in
`references/clustering-rubric.md` is unchanged.

- `mechanisms_claimed`: one entry per member, `{reviewer, member_id,
  mechanism}` where `mechanism` is one sentence restating what that
  reviewer says causes the symptom, in the reviewer's own terms.
- `mechanism_agreement`: `compatible | split | unknown`, required when
  the cluster has more than one member.

`validate_partition.py` enforces presence and enum values. Missing
values on multi-member clusters are a validation error, same as a
missing `match_rationale`.

### 6.4 Scoring additions (code, deterministic)

`validate_partition.py` adds to each cluster:

- `lane` from quorum as in 6.2.
- `validation`: `{mode}` from the lane; for solo clusters the judge may
  supply an optional `cheapest_check` hint, passed through.
- `locations`: the union of every member's anchored location plus any
  additional locations parsed from the member body. Bugbot embeds
  `LOCATIONS START ... LOCATIONS END` blocks with extra file and line
  ranges; those are parsed. Each entry carries `from: <member_id>`.
- `p_tag`: `P1` for severity critical or major, `P2` for minor, `P3` for
  nit. Severity words are kept; the P-tag is a display shorthand.
- `stated_severity` on each finding, parsed deterministically in
  `fetch_findings.sh` from the reviewer's own markers: Bugbot's
  `**Low|Medium|High Severity**`, Greptile's `P1|P2|P3`, Devin's
  `(bug)` versus `(analysis)`. Mapping to `critical|major|minor|nit` is
  a table in one place. Unknown formats yield no value. The clustering
  judge sees it as a hint; generated singletons (fast path and
  all-singletons fallback) take their severity from it instead of the
  hard-coded `minor`, so the merge gate is meaningful on single-reviewer
  PRs. A validator may correct severity with `dispose --severity`.

The document gains `schema: "quorum/triage@1"`, `pr`, `head` (the PR
head SHA at fetch time), `reviewers` (counts per reviewer), `lanes`
(ids per lane), `priors` (may be empty), `deferred` (derived from
dispositions), and `timeline` (see 6.8).

### 6.5 Human brief

Rendered at the top of the comment, grouped by lane. Order within a lane
is severity, then file. Owner decision: group by lane.

```
🤝 Quorum · triage brief          updated after <sha> · head <sha> · N reviewers

<merge gate line>                                      (section 6.6)
**14 findings** from Bugbot, Devin, Greptile → **12 issues** · 7 valid · 4 partly · 1 dismissed · 8 deferred

### Agreed · 2 of 3 reviewers — presumed real, escalated
- **P1** Backlog slice from `Started.atMs` omits the pre-roll · Bugbot, Devin
  ✅ valid · fixed ba814e4 · <note>
  Invariant: <one sentence>
  <mermaid invariant map, after escalation>
- **P1** ...

### Solo · 1 reviewer — prove or kill
- **P2** <title> · Devin
  ⚠ partly · fixed ba814e4 · <note>
- ...
- — <title> · Devin
  ✖ dismissed · <note>

### Deferred by owner · 8
<comma-separated titles>

<details><summary>🤖 For triage agents · protocol, plan and ledger</summary>
  ... (section 6.7)
</details>
<sub>Quorum · plan 13:02 → validated 13:31 → fixed 13:44 · quorum/triage@1</sub>
```

Before validation, each row's disposition line reads `→ escalate ·
invariant · sibling sweep · fix brief` or `→ prove or kill`, and the
invariant map slot is a one-line placeholder.

Each row links each member id to its original comment, as today. The
👀 reactions on members of agreed clusters stay.

### 6.6 Merge gate

Replaces any confidence score. Derived in code from dispositions.

A cluster is **gate-relevant** when its severity is `critical` or
`major`. Agreed clusters at minor or nit severity are shown as agreed
but never block on their own.

A gate-relevant cluster **blocks** when either: it has no disposition
yet and triage has started; or its latest verdict is `valid` or `partly`
with no `commit` recorded. A `deferred` or `invalid` verdict never
blocks. A `stale` disposition counts as no disposition.

| State | Condition | Line |
|---|---|---|
| pending | zero dispositions on the PR | ⏳ Triage pending · N agreed presumed real · M solo queued |
| blocked | at least one blocking cluster | 🛑 Blocked by k issue(s) · j still pending review · <titles> |
| clear | every gate-relevant cluster disposed, none blocking | ✅ Merge gate clear · was blocked by k · fixed in <commits> · <tests note if given> |

The headline can therefore never read "clear" while an untouched P1
remains, which is what the owner saw in the mockup.

### 6.7 Agent layer

A collapsed `<details>` block at the bottom of the same comment, with
two parts.

**Protocol**, fixed text embedded by the renderer from
`references/triage-protocol.md` so the skill and the comment never
disagree. Draft text, to be kept to roughly five lines:

1. `agreed` clusters: do not re-prove. Establish the missing invariant,
   sweep for sibling sites, write a fix brief naming the chokepoint and
   the test.
2. `solo` clusters: prove or kill. Cheapest reproduction first. Return
   `valid | partly | invalid` with evidence.
3. If `mechanism_agreement` is `split`, establish the true mechanism
   before fixing anything.
4. Skip clusters that already carry a disposition unless `head` moved and
   touched their files (`stale: true`).
5. Report with the disposition script (6.8). Quorum re-renders this
   comment. Also reply in the original thread. The script comes from a
   Quorum checkout (`scripts/quorum_ledger.py` in
   `github.com/Wenjix/quorum`) or, without one, from the pinned tag the
   protocol names:
   `gh api repos/Wenjix/quorum/tarball/<tag> > quorum.tgz && tar -xzf quorum.tgz`.
   The tarball rather than a single file, because the ledger script
   imports sibling modules and reads the protocol reference. This works
   whether the repo is public or private, since the agent already has
   `gh` auth for the PR. Tagging that release is part of this work.

**Triage JSON**, `quorum/triage@1`, in a fenced `json` block. Top level:

```
schema, pr, head, generated_at, totals, reviewers, priors, lanes,
clusters[], deferred[], timeline, orphaned_dispositions[]
```

Per cluster, in addition to every field in today's scored cluster:

```
lane, p_tag, symptom, mechanisms_claimed[], mechanism_agreement,
locations[], validation {mode, cheapest_check?},
escalation? {verdict, invariant, siblings[{file, lines, risk, rationale}],
             chokepoint, test, evidence[],
             map? {nodes[{id, label, kind}], edges[[from, to]]}, by, at},
disposition? {verdict, note, evidence[], members? {member_id: verdict},
              severity?, commit?, tests?, by, at, stale?},
disposition_history[]
```

The lists above are the required and recognised fields, not an
exhaustive schema; the builder may add fields under a `x_` prefix.

`escalation.map` is optional. `kind` is one of `source | break |
symptom | fix | sibling`. When present, the renderer draws it as the
mermaid invariant map, which is the data-flow chain the owner accepted
in the mockup. When absent, the renderer draws a plainer star:
`locations` and `siblings` pointing at the `chokepoint`.

`disposition.members` lets a verdict differ per reviewer inside one
cluster, which matters on `split` clusters where one reviewer's
mechanism was right and another's was not. Members not listed inherit
the cluster verdict. Priors (section 9) are computed from member-level
verdicts, so per-bot precision is credited correctly.

`priors` is keyed by reviewer: `{valid, partly, invalid, n}` as fractions
and a count, computed from past ledgers in the same repo (section 9).
Empty object when unknown. The protocol states it is a prior, not a
verdict.

### 6.8 Dispositions and the ledger script

One script owns every write to the comment. Working name
`scripts/quorum_ledger.py`; the builder may split it, but every path must
re-render from JSON.

```
quorum_ledger.py post-plan   OWNER/REPO PR clusters.scored.json [--dry-run]
quorum_ledger.py dispose     OWNER/REPO PR CLUSTER_ID
                             --verdict valid|partly|invalid|deferred
                             --note "..." [--evidence "..."]...
                             [--member ID:verdict]... [--severity LEVEL]
                             [--commit SHA] [--tests "..."] [--by NAME] [--reply]
quorum_ledger.py dispose     OWNER/REPO PR --from dispositions.json
quorum_ledger.py escalate    OWNER/REPO PR CLUSTER_ID --from escalation.json
                             # writes the escalation block AND a disposition
                             # from escalation.verdict in one update
quorum_ledger.py show        OWNER/REPO PR            # prints current JSON
```

Transport: the script uses `gh` when present, otherwise plain HTTPS with
`GITHUB_TOKEN`, so the Node CLI's token-only path keeps working. The
Node CLI shells out to this script for every comment write, as
`synthesizeCommand` already does for `post_synthesis.py`.

Rules:

- `dispose` with a new verdict and no `--note` is an error. A call that
  only adds `--commit` or `--tests` to an existing disposition keeps the
  prior verdict and note. `--verdict` of anything other than `invalid`
  or `deferred` combined with no `--evidence` is a warning in the output
  but accepted; the human brief marks it "no evidence recorded".
- The latest disposition is rendered; all are kept in
  `disposition_history`.
- `--reply` posts the verdict and note as a reply in each member's
  original thread. This matches what every session has done by hand.
- `timeline` records `plan_at`, `first_disposition_at`,
  `last_disposition_at`, `fixed_at`. The footer shows them. This is the
  "time to first validation" measurement the owner asked about, for free.

### 6.9 Identity across re-runs

Bots re-review after pushes, the PR head moves, and re-clustering can
produce different cluster ids. Dispositions must survive.

- Finding identity is the GitHub review comment id, which is stable.
- A new cluster inherits the latest disposition of any prior cluster
  that shares at least one member comment id. If it inherits from more
  than one, the renderer keeps the most recent and lists the others in
  `disposition_history`.
- If `head` moved since the disposition and the cluster's `locations`
  files are in the diff between the two heads, the disposition is marked
  `stale: true` and the human brief shows "re-check" instead of the
  verdict pill. The owner's own fix push is the exception: a disposition
  whose recorded `commit` is the new head or one of the commits between
  the two heads stays live.
- Prior dispositions with no successor cluster go to
  `orphaned_dispositions` in the JSON and are not shown in the human
  brief.

### 6.10 Single-reviewer fast path

If `fetch_findings.sh` reports one distinct reviewer, the skill skips
the rubric and the LLM clustering pass entirely. `validate_partition.py`
is run on a generated all-singletons `clusters.json` (the CLI already
has this code path in `synthesizeCommand`). Every cluster is `solo`. The
plan is posted so the ledger and merge gate still exist. A `--no-comment`
flag skips posting for owners who want nothing on single-reviewer PRs.

### 6.11 Who does what

Owner decision, verbatim: "Correct. The former."

- **Quorum plans and keeps the ledger.** Fetch, cluster, score, post the
  plan, accept dispositions, re-render.
- **Whichever agent is in the session does the triage**, by reading the
  agent layer. Claude Code, Devin CLI, Cursor.
- **In Claude Code, the skill offers to drive the triage** after posting
  the plan: it dispatches read-only subagents per lane, collects their
  structured results, and calls the ledger script. It then offers fixes,
  which remain the owner's call.

### 6.12 Skill changes (`SKILL.md`)

- Step 0: reviewer count check and the single-reviewer fast path.
- Steps 1 to 3: unchanged, plus the two new rubric fields.
- Step 4: `post-plan --dry-run`, show the owner in an interactive
  session, then post. Non-interactive posts directly, as today.
- Step 5, new: offer to drive. Per `agreed` cluster, one escalation
  subagent with the root-cause and pattern-sweep prompt shapes already in
  `src/prompts.ts`, returning `{invariant, siblings, chokepoint, test}`
  plus the existing mechanism and evidence fields. Per `solo` cluster,
  one validator subagent (batched when many) returning `{verdict,
  evidence, note, mechanism_correction?}`. All read-only. The skill calls
  `quorum_ledger.py` with the results.
- Step 6: offer fixes. After fixes, `dispose --commit`.
- Dogfood log: unchanged, plus the final triage JSON.
- `QUORUM_BOTS` default gains `greptile` and `coderabbit`.

### 6.13 CLI changes (`src/`)

Owner decision, verbatim: "let's go with B". This round changes the
Node CLI only as far as needed so nothing breaks and the skill path is
complete. The cloud runner has not been used since June; the
interactive loop is where the value has been.

In this round:

- `parseScoredClusters` and `recoverScoredClustersFromPullRequest`
  accept `quorum/triage@1` as well as the old shape.
- `synthesize` renders the new comment through the ledger script.
- `run-pr`, `post-pr`, `triage-pr` select by `lane == "agreed"` by
  default; `--min-quorum` and `--cluster` stay as overrides. Their
  outputs are otherwise unchanged: local artifacts, Canvas, `eval`, and
  the existing `<!-- quorum:exploration -->` comment from `post-pr`.
- New `quorum priors OWNER/REPO`: section 9. It is independent of the
  runner and may be sequenced last.

Deferred to a follow-up after the interactive loop has been dogfooded
on a few PRs, and listed again in section 10:

- `post-pr` writes escalations into the one comment via `escalate`, and
  the exploration comment is retired.
- A `validate` task type in the DAG for solo clusters selected by
  override, written via `dispose`.

Until then the one-comment rule binds the skill path, which never writes
a second comment; a `post-pr` run still adds its own.

## 7. What the human sees, state by state

The owner saw and accepted a mockup of both states on 2026-10-28; the
file is at
`.superpowers/brainstorm/86583-1793256608/content/comment-mockup.html`.

| State | Gate line | Row disposition line | Invariant map |
|---|---|---|---|
| plan | ⏳ Triage pending | → escalate / → prove or kill | placeholder line |
| validated | 🛑 Blocked by k | verdict pill + note | present for escalated clusters |
| fixed | ✅ Merge gate clear | verdict pill + fixed `sha` + note | present |

## 8. Decisions the owner keeps

Same two as today.

1. Whether the plan is posted. Interactive sessions show the dry run
   first unless the owner already said to post.
2. Whether fixes are made. The fix brief is a proposal until the owner
   says go, or has said "triage and fix" up front.

Everything else, including dispositions written by validators, proceeds
without asking.

## 9. Priors

Per-bot precision by lane, computed from past ledgers in the same repo.
The ledgers are the dataset; nothing else needs to be stored.

- `quorum priors OWNER/REPO` searches issue comments carrying the marker
  across the repo's PRs, parses each triage JSON, and aggregates
  dispositions by reviewer and lane. Output is cached at
  `.quorum/priors/<owner>-<repo>.json` and embedded in the next plan when
  present and less than seven days old.
- Below five dispositions for a reviewer, that reviewer's prior is
  omitted rather than shown.
- The dogfood log at `.quorum/log/` remains a local backup source.

## 10. Later, explicitly out of this design

- Cloud runner write-back into the ledger, a `validate` DAG task for
  solo clusters, and retiring the exploration comment (owner chose
  option B in 6.13).
- Reading owner replies in bot threads into dispositions, the Greptile
  "Acknowledged by" behaviour. Needs a convention or a model read of
  free text; the ledger schema already has a `by: owner` slot for it.
- Counting a reviewer that reviewed and found nothing in the quorum
  denominator. Needs review-event data, not just comments.
- Cross-PR pattern sweeps.

## 11. Left to the builder

- Exact markdown rendering within GitHub's constraints, emoji choices,
  and how member links are shown.
- Comment size strategy. Suggested order: drop `members[].url` and
  `hunk`-derived text first, then truncate `escalation.siblings` to the
  top ten by risk, then move the full JSON to a gist or workflow artifact
  with a pointer while keeping protocol, lanes, and per-cluster
  essentials inline.
- Whether the ledger script is one file with subcommands or several.
- Validator and escalation subagent prompt wording, within the shapes in
  6.12 and the existing `src/prompts.ts`.
- Mermaid layout for the invariant map and the bullet-list fallback.
- Test strategy. Minimum: node tests for schema recovery and lane
  selection; python tests for rendering determinism, disposition
  inheritance across re-clustering, and the merge gate rules.
- Migration of an existing old-format synthesis comment on first write.
- The release tag name and whether `quorum.skill` is attached to the
  GitHub release.

## 12. Acceptance

- On a multi-reviewer PR, the plan comment appears before any validation
  runs, with agreed rows marked escalate and solo rows marked prove or
  kill.
- After triage, every cluster has a disposition with a note, the merge
  gate reflects the blocking rule, and the PR carries exactly one Quorum
  comment.
- On a single-reviewer PR, no rubric read and no LLM clustering pass
  happens.
- A re-run after a push keeps every disposition; touched clusters show
  re-check.
- The `timeline` footer shows plan-to-first-disposition on the next
  three dogfood PRs, so the cost question has a number.
- An agent without the skill, given only the PR, can read the agent
  layer and produce a valid `dispose` call.

## 13. Decision log

| Decision | By |
|---|---|
| Quorum becomes a triage director; agreement directs validation effort | owner ("Yes") |
| One comment, human brief on top, agent layer collapsed below | owner |
| Human brief grouped by lane | owner (chose A in mockup) |
| Quorum plans and keeps the ledger; session agent triages; Claude Code skill offers to drive | owner ("Correct. The former.") |
| Two lanes plus a mechanism flag, not three lanes | agent |
| Escalation runs inline in the session; cloud DAG becomes opt-in and writes into the same ledger | agent |
| Merge gate instead of confidence score, with the blocking rule in 6.6 | agent |
| Dispositions written only by script; thread-reply parsing deferred | agent |
| Deferred items are a disposition verdict | agent |
| Priors slot now, data from ledgers later | agent |
| Single-reviewer fast path posts a ledger unless `--no-comment` | agent |
| Greptile and CodeRabbit in the default bot list | agent |
| Agreed clusters get their verdict from the escalation result, default valid; no separate validator | agent, after builder check |
| Gate cannot read clear while a gate-relevant cluster is undisposed | agent, after builder check |
| Per-member verdict overrides; priors from member level | agent, after builder check |
| Stated severity parsed from bot markers so fast-path gates are meaningful | agent, after builder check |
| Ledger script fetched via `gh api` at a pinned tag; this work tags a release | agent, after builder check |
| Optional `escalation.map` for the invariant map, star fallback | agent, after builder check |
| Runner scope: option B, schema compatibility only this round | owner ("let's go with B") |

## 14. Open for the owner

Nothing. The runner-scope question was resolved on 2026-10-29 with
option B.
