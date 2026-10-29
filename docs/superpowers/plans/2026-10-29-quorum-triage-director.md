# Quorum Triage Director Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the Quorum skill into a triage director: reviewer agreement assigns each cluster a lane, one upserted PR comment carries a human brief and an agent layer, and a single ledger script owns every write.

**Architecture:** The skill path stays bash + python3 + gh + jq. `fetch_findings.sh` gains stated severity and PR head; `validate_partition.py` adds lanes, locations and the `quorum/triage@1` envelope; four new pure python modules (doc, gate, merge, render) feed one CLI, `quorum_ledger.py`, which replaces `post_synthesis.py`. The Node CLI only learns to read the new schema, shell out to the ledger script, and compute priors.

**Tech Stack:** bash, jq 1.6+, python3 (stdlib only, `unittest`), Node 22 + TypeScript (existing `node --test` suite), `gh` CLI.

**Spec:** `docs/superpowers/specs/2026-10-28-quorum-triage-director-design.md`

**Branch:** create `feat/triage-director` from `docs/quorum-triage-director-design` so the spec travels with the code.

## Global Constraints

- Node `>=22` (package.json engines). Skill scripts: bash, python3 stdlib, `gh`, `jq` only; the Node CLI may wrap them but is never required by the skill.
- Quorum is computed in code, never by the model. Precision over recall on merges. Clustering failure never blocks (fallback: all singletons).
- Every PR comment write goes through `scripts/quorum_ledger.py`; rendered markdown is a deterministic function of the embedded JSON.
- Comment marker stays `<!-- quorum:synthesis -->`. GitHub cap 65,536 chars; working limit `MAX_BODY = 60000`.
- Triage JSON schema id `quorum/triage@1` is a superset of today's `clusters.scored.json`: keep `totals`, `generated_at`, every existing cluster field, and `members[]` with `id, reviewer, file, lines, url, comment_id, node_id, outdated`.
- Lanes: `agreed` when `quorum >= 2`, else `solo`. `p_tag`: critical, major → `P1`; minor → `P2`; nit → `P3`.
- Gate-relevant severity set: `{"critical", "major"}`.
- Stated severity table: Bugbot `**High Severity**` → critical, `**Medium Severity**` → major, `**Low Severity**` → minor; Devin id prefix `BUG_` → major, `ANALYSIS_` → minor; Greptile `P0`→critical, `P1`→major, `P2`→minor, `P3`→nit (Greptile pattern is an assumption, see Task 1).
- Default bot regex: `cursor\[bot\]|copilot|devin|greptile|coderabbit`.
- Mermaid invariant map at most 12 nodes; beyond that a bullet list.
- Priors: omit a reviewer with fewer than `MIN_N = 5` dispositions; cache fresh for `MAX_AGE_DAYS = 7`.
- Ledger script version pin `LEDGER_REF = "v0.3.0"`; package version `0.3.0`.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A bot comment with no recognised severity marker and no location block (Copilot, CodeRabbit): `stated_severity` must be `null`, `locations` must be the anchored location only, nothing crashes. Test in Task 1.
2. Re-running `post-plan` when the existing comment is the old format (no `schema` field): treated as no prior dispositions and upgraded in place, never a second comment. Test in Task 5.
3. The `compare` API failing (old head force-pushed away): `changed_files` is unknown, so every carried disposition is marked stale rather than silently trusted. Test in Task 5.
4. `dispose` for a `cluster_id` not in the ledger: exit non-zero with the id named, no comment write. Test in Task 8.
5. A rendered comment over 60,000 chars: shrink levels apply in order and the Node CLI still recovers the JSON or fails with the existing clear error. Tests in Task 6 and Task 10.

---

### Task 1: Normalised findings with stated severity and PR head

**Files:**
- Create: `scripts/normalize_findings.jq`
- Modify: `scripts/fetch_findings.sh`
- Create: `scripts/tests/test_normalize_findings.py`
- Create: `scripts/tests/fixtures/raw_comments.json`
- Modify: `package.json` (`test`, `build:skill`)

**Interfaces:**
- Produces: finding records as today plus `pr: "OWNER/REPO#N"`, `head: "<sha>"`, `stated_severity: "critical"|"major"|"minor"|"nit"|null`. Short names add `greptile` and `coderabbit`.
- Produces: the jq program is invoked as `jq --arg re "$BOTS_RE" --arg pr "$REPO#$PR" --arg head "$HEAD" -f scripts/normalize_findings.jq raw.json`.
- Produces: python tests run with `python3 -m unittest discover -s scripts/tests -v`; each test file starts with `sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))`.

- [ ] **Step 1: Write the fixture and the failing test**

`scripts/tests/fixtures/raw_comments.json`: an array of four GitHub review-comment objects (fields `id, node_id, user.login, path, line, start_line, original_line, body, diff_hunk, html_url`): one `cursor[bot]` with `**Medium Severity**` and a `<!-- LOCATIONS START\nsrc/a.ts#L10-L12\nLOCATIONS END -->` block, one `devin-ai-integration[bot]` whose body starts `<!-- devin-review-comment {"id": "BUG_x", ...} -->`, one `greptile-apps[bot]` whose body contains `P1`, one `Copilot` with a plain body and `line: null`.

```python
class NormalizeFindingsTest(unittest.TestCase):
    def run_jq(self, bots_re=r"cursor\[bot\]|copilot|devin|greptile|coderabbit"):
        out = subprocess.run(["jq", "--arg", "re", bots_re, "--arg", "pr", "o/r#7",
                              "--arg", "head", "abc1234", "-f", JQ, FIXTURE],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def test_stated_severity_per_reviewer(self):
        by = {f["reviewer"]: f for f in self.run_jq()}
        self.assertEqual(by["bugbot"]["stated_severity"], "major")
        self.assertEqual(by["devin"]["stated_severity"], "major")
        self.assertEqual(by["greptile"]["stated_severity"], "major")
        self.assertIsNone(by["copilot"]["stated_severity"])

    def test_pr_head_and_short_names(self):
        rows = self.run_jq()
        self.assertTrue(all(r["pr"] == "o/r#7" and r["head"] == "abc1234" for r in rows))
        self.assertEqual(sorted(r["reviewer"] for r in rows), ["bugbot", "copilot", "devin", "greptile"])

    def test_outdated_copilot_keeps_null_lines(self):
        cop = next(r for r in self.run_jq() if r["reviewer"] == "copilot")
        self.assertTrue(cop["outdated"])
        self.assertEqual(cop["lines"], [None, None])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest scripts.tests.test_normalize_findings -v` (from repo root)
Expected: FAIL, `scripts/normalize_findings.jq` not found.

- [ ] **Step 3: Move the jq filter out of `fetch_findings.sh` into `scripts/normalize_findings.jq` and extend it**

Keep the existing `short(l)` and record shape. Add `def stated(reviewer; body)` implementing the stated-severity table from Global Constraints (Greptile pattern `\bP([0-3])\b` on the first 200 chars; comment in the file that this is an assumption until a real Greptile line comment is checked). Add `pr: $pr`, `head: $head`, `stated_severity: stated(.reviewer; .body)` to each record. `short` maps `greptile` and `coderabbit` logins to those names.

- [ ] **Step 4: Update `fetch_findings.sh`**

Default `BOTS_RE` becomes the Global Constraints regex. Add `HEAD=$(gh api "repos/$REPO/pulls/$PR" --jq .head.sha)`. Replace the inline jq program with `jq --arg re "$BOTS_RE" --arg pr "$REPO#$PR" --arg head "$HEAD" -f "$(dirname "$0")/normalize_findings.jq" "$RAW" > "$OUT"`.

- [ ] **Step 5: Wire python tests into npm and keep them out of the skill zip**

`package.json`: `"test": "npm run build && node --test dist/test/*.test.js && python3 -m unittest discover -s scripts/tests -v"`. In `build:skill`, after the `cp -r scripts references ...`, add `&& rm -rf .skill-build/quorum/scripts/tests .skill-build/quorum/scripts/__pycache__`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `npm test`
Expected: node suite PASS, python suite `OK` with 3 tests.

- [ ] **Step 7: Commit**

```bash
git add scripts/normalize_findings.jq scripts/fetch_findings.sh scripts/tests package.json
git commit -m "feat(skill): stated severity, PR head and new bots in fetched findings"
```

---

### Task 2: `validate_partition.py` emits `quorum/triage@1` with lanes and locations

**Files:**
- Modify: `scripts/validate_partition.py`
- Modify: `references/clustering-rubric.md`
- Create: `scripts/tests/test_validate_partition.py`
- Create: `scripts/tests/fixtures/findings.json`, `scripts/tests/fixtures/clusters.json`

**Interfaces:**
- Consumes: findings records from Task 1.
- Produces: output document with top-level `schema: "quorum/triage@1"`, `pr`, `head`, `generated_at`, `totals`, `reviewers: {name: count}`, `priors: {}`, `lanes: {agreed: [ids], solo: [ids]}`, `clusters`, `deferred: []`, `timeline: {}`, `orphaned_dispositions: []`.
- Produces per cluster, in addition to today's fields: `lane`, `p_tag`, `mechanisms_claimed: [{reviewer, member_id, mechanism}]` (default `[]`), `mechanism_agreement` (only when more than one member), `locations: [{file, lines: [a, b], from}]`, `validation: {mode: "escalate"|"prove_or_kill", cheapest_check?}`, `disposition_history: []`. Optional judge fields `symptom` and `cheapest_check` pass through unvalidated.
- Produces: new flags `--all-singletons` (ignore `clusters.json`, build one singleton per finding), `--priors PATH` (JSON merged into `priors`, default `{}`).
- Produces functions: `lane_for(quorum: int) -> str`, `parse_extra_locations(body: str) -> list[dict]`, `union_locations(cluster: dict, fmap: dict) -> list[dict]`, `singletons_doc(findings: list) -> dict`.

- [ ] **Step 1: Write fixtures and failing tests**

`fixtures/findings.json`: four findings (`bugbot-1` body with the Bugbot LOCATIONS block and `stated_severity: "major"`, `devin-1` with `stated_severity: "major"`, `devin-2` with `null`, `greptile-1` with `"minor"`), all with `pr: "o/r#7"`, `head: "abc1234"`. `fixtures/clusters.json`: `c1 = [bugbot-1, devin-1]` with `mechanisms_claimed` for both and `mechanism_agreement: "split"`, `c2 = [devin-2]`, `c3 = [greptile-1]`.

```python
def test_lanes_p_tags_and_envelope(self):
    doc = run_validate(FINDINGS, CLUSTERS)
    self.assertEqual(doc["schema"], "quorum/triage@1")
    self.assertEqual((doc["pr"], doc["head"]), ("o/r#7", "abc1234"))
    self.assertEqual(doc["lanes"], {"agreed": ["c1"], "solo": ["c2", "c3"]})
    c1 = by_id(doc, "c1")
    self.assertEqual((c1["lane"], c1["p_tag"], c1["validation"]["mode"]), ("agreed", "P1", "escalate"))
    self.assertEqual(by_id(doc, "c3")["validation"], {"mode": "prove_or_kill"})
    self.assertEqual(doc["reviewers"], {"bugbot": 1, "devin": 2, "greptile": 1})
    self.assertEqual(doc["priors"], {})

def test_locations_union_includes_bugbot_block(self):
    locs = by_id(run_validate(FINDINGS, CLUSTERS), "c1")["locations"]
    self.assertIn({"file": "src/a.ts", "lines": [10, 12], "from": "bugbot-1"}, locs)
    self.assertEqual(len(locs), 3)  # anchored bugbot-1, anchored devin-1, extra block

def test_multi_member_cluster_requires_mechanism_fields(self):
    bad = strip_fields(CLUSTERS, "c1", ["mechanism_agreement"])
    rc, err = run_validate_rc(FINDINGS, bad)
    self.assertEqual(rc, 1)
    self.assertIn("c1: mechanism_agreement", err)

def test_all_singletons_uses_stated_severity(self):
    doc = run_validate(FINDINGS, None, extra=["--all-singletons"])
    sev = {c["member_ids"][0]: c["severity"] for c in doc["clusters"]}
    self.assertEqual(sev, {"bugbot-1": "major", "devin-1": "major", "devin-2": "minor", "greptile-1": "minor"})
    self.assertTrue(all(c["lane"] == "solo" for c in doc["clusters"]))

def test_priors_file_is_embedded(self):
    doc = run_validate(FINDINGS, CLUSTERS, extra=["--priors", PRIORS_FIXTURE])
    self.assertIn("bugbot", doc["priors"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_validate_partition -v`
Expected: FAIL on `schema` KeyError.

- [ ] **Step 3: Implement the additions in `scripts/validate_partition.py`**

Add constants `SCHEMA = "quorum/triage@1"`, `P_TAG`, `LANES`, `MECHANISM_AGREEMENT = {"compatible", "split", "unknown"}`. In `validate()`, for clusters with more than one member require `mechanisms_claimed` (list with one entry per member id, each with non-empty `reviewer`, `member_id`, `mechanism`) and `mechanism_agreement` in the enum; error strings `"{cid}: mechanisms_claimed must have one entry per member"` and `"{cid}: mechanism_agreement '{v}' not in [...]"`. `parse_extra_locations` matches lines `path#L10-L12` or `path#L10` inside `LOCATIONS START ... LOCATIONS END`. `singleton_from` severity: `parent_severity or finding.get("stated_severity") or "minor"`. `singletons_doc` mirrors `generateSingletonClusters` in `src/cli.ts`. After scoring, set `lane`, `p_tag`, `locations`, `validation`, `disposition_history`, default `mechanisms_claimed: []`, and build the envelope; `pr` and `head` come from `findings[0]` when present.

- [ ] **Step 4: Add the rubric fields and a worked example**

In `references/clustering-rubric.md` output schema, add `mechanisms_claimed` and `mechanism_agreement` with one sentence each, and worked example **D — same symptom, different mechanism** (two bots on one pipe-write bug, one says partial writes, the other says whole-frame drops; merge, `mechanism_agreement: "split"`). State that this is matching, not re-reviewing.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.tests.test_validate_partition -v`
Expected: `OK`, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/validate_partition.py references/clustering-rubric.md scripts/tests
git commit -m "feat(skill): quorum/triage@1 envelope with lanes, locations and mechanism fields"
```

---

### Task 3: `quorum_doc.py` and `quorum_gate.py` (pure helpers)

**Files:**
- Create: `scripts/quorum_doc.py`
- Create: `scripts/quorum_gate.py`
- Create: `scripts/tests/test_gate.py`
- Create: `scripts/tests/fixtures/triage.json` (a complete `quorum/triage@1` doc: `c1` agreed/major, `c2` solo/major, `c3` solo/minor, `c4` agreed/minor; no dispositions)

**Interfaces:**
- Produces `quorum_doc.py`: `SCHEMA`, `MARKER = "<!-- quorum:synthesis -->"`, `extract_doc(body: str) -> dict | None` (last fenced ```json block that parses and has `clusters`), `is_triage(doc) -> bool` (`schema == SCHEMA`), `upgrade_legacy(doc) -> dict` (adds `schema`, `lanes`, per-cluster `lane`, `p_tag`, `locations` from members, empty `disposition_history`, and the other envelope fields to an old scored doc), `latest_disposition(cluster) -> dict | None`, `now_iso() -> str`.
- Produces `quorum_gate.py`: `GATE_RELEVANT = {"critical", "major"}`, `is_disposed(cluster) -> bool` (has disposition and not `stale`), `triage_started(doc) -> bool`, `blocks(cluster, started: bool) -> bool`, `compute_gate(doc) -> dict` with keys `state` (`"pending"|"blocked"|"clear"`), `blocking: [ids]`, `pending_review: [ids]`, `was_blocked_by: int`, `fixed_commits: [sha]`, `tests: str | None`; `compute_deferred(doc) -> [ids]`; `compute_timeline(doc, previous: dict | None) -> dict` with `plan_at`, `first_disposition_at`, `last_disposition_at`, `fixed_at` (ISO strings or `None`; `plan_at` carried from `previous` when present).

- [ ] **Step 1: Write the failing tests**

```python
def test_pending_when_no_dispositions(self):
    g = compute_gate(load("triage.json"))
    self.assertEqual(g["state"], "pending")
    self.assertEqual(g["blocking"], [])

def test_undisposed_major_blocks_once_started(self):
    doc = with_disposition(load("triage.json"), "c3", verdict="invalid", note="n")
    g = compute_gate(doc)
    self.assertEqual(g["state"], "blocked")
    self.assertEqual(sorted(g["blocking"]), ["c1", "c2"])
    self.assertEqual(sorted(g["pending_review"]), ["c1", "c2"])

def test_valid_without_commit_blocks_and_with_commit_clears(self):
    doc = load("triage.json")
    for cid in ("c1", "c2"):
        doc = with_disposition(doc, cid, verdict="valid", note="n")
    self.assertEqual(compute_gate(doc)["state"], "blocked")
    for cid in ("c1", "c2"):
        doc = with_disposition(doc, cid, verdict="valid", note="n", commit="ba814e4")
    g = compute_gate(doc)
    self.assertEqual((g["state"], g["was_blocked_by"], g["fixed_commits"]), ("clear", 2, ["ba814e4"]))

def test_deferred_and_minor_never_block(self):
    doc = with_disposition(load("triage.json"), "c1", verdict="deferred", note="later")
    doc = with_disposition(doc, "c2", verdict="invalid", note="no")
    self.assertEqual(compute_gate(doc)["state"], "clear")
    self.assertEqual(compute_deferred(doc), ["c1"])

def test_stale_disposition_counts_as_undisposed(self):
    doc = with_disposition(load("triage.json"), "c2", verdict="valid", note="n", commit="x", stale=True)
    doc = with_disposition(doc, "c1", verdict="valid", note="n", commit="y")
    self.assertIn("c2", compute_gate(doc)["pending_review"])

def test_extract_doc_and_legacy_upgrade(self):
    body = "x\n```mermaid\nflowchart LR\n```\n```json\n" + json.dumps(LEGACY_SCORED) + "\n```"
    doc = upgrade_legacy(extract_doc(body))
    self.assertEqual(doc["schema"], "quorum/triage@1")
    self.assertEqual(doc["clusters"][0]["lane"], "agreed")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_gate -v`
Expected: FAIL, `ModuleNotFoundError: quorum_gate`.

- [ ] **Step 3: Implement `scripts/quorum_doc.py` and `scripts/quorum_gate.py`**

Rules from spec 6.6: a gate-relevant cluster blocks when (undisposed and `triage_started`) or (latest verdict in `{valid, partly}` and no `commit`); `deferred` and `invalid` never block; stale counts as undisposed. `state`: `pending` when no cluster is disposed; `blocked` when any blocks; else `clear`. `was_blocked_by` counts gate-relevant clusters whose latest verdict is `valid` or `partly`. `tests` is the last non-empty `disposition.tests`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.tests.test_gate -v`
Expected: `OK`, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/quorum_doc.py scripts/quorum_gate.py scripts/tests
git commit -m "feat(skill): triage doc helpers and merge-gate rules"
```

---

### Task 4: `quorum_github.py` transport with an injectable client

**Files:**
- Create: `scripts/quorum_github.py`
- Create: `scripts/tests/test_github.py`

**Interfaces:**
- Produces: `class Client(Protocol)` with `api(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any` and `graphql(self, query: str, variables: dict) -> Any`; `class GhClient` (subprocess `gh api`, `--paginate` when `params.get("paginate")`); `class TokenClient(token: str)` (urllib to `https://api.github.com`); `default_client() -> Client` (gh if `shutil.which("gh")`, else `GITHUB_TOKEN`/`GH_TOKEN`, else `RuntimeError("No gh CLI and no GITHUB_TOKEN")`).
- Produces functions, all taking `client` first: `get_pr_head(client, repo, pr) -> str`; `find_marker_comment(client, repo, pr, marker) -> dict | None` (`{"id", "body"}`, newest match); `upsert_comment(client, repo, pr, body, existing_id=None) -> int`; `add_eyes(client, repo, comment_id) -> bool`; `minimize(client, node_id) -> bool`; `compare_files(client, repo, base, head) -> list[str] | None` (`None` on any error); `reply_in_thread(client, repo, pr, comment_id, body) -> bool`.
- Produces for tests: `class FakeClient` in `scripts/tests/fakes.py` recording `calls: list[tuple]` and answering from a `responses: dict[(method, path_prefix)]` map.

- [ ] **Step 1: Write the failing tests**

```python
def test_find_marker_comment_returns_newest_match(self): ...   # two matching comments, higher id wins
def test_upsert_patches_when_existing(self): ...              # ("PATCH", "repos/o/r/issues/comments/5") recorded
def test_upsert_posts_when_absent(self): ...                   # ("POST", "repos/o/r/issues/7/comments")
def test_compare_files_none_on_error(self): ...                # FakeClient raises -> None
def test_reply_in_thread_path(self): ...                       # POST repos/o/r/pulls/7/comments/99/replies
def test_default_client_prefers_gh(self): ...                  # monkeypatch shutil.which
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_github -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `scripts/quorum_github.py`**

GraphQL minimize mutation is the one already in `post_synthesis.py`. `TokenClient.graphql` posts to `https://api.github.com/graphql`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.tests.test_github -v`
Expected: `OK`, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/quorum_github.py scripts/tests
git commit -m "feat(skill): GitHub transport with gh and token clients"
```

---

### Task 5: `quorum_merge.py` carries dispositions across re-runs

**Files:**
- Create: `scripts/quorum_merge.py`
- Create: `scripts/tests/test_merge.py`

**Interfaces:**
- Consumes: `quorum_doc.upgrade_legacy`, `quorum_doc.latest_disposition`.
- Produces: `member_comment_ids(cluster) -> set[int]`; `inherit(new_doc: dict, prior_doc: dict | None, changed_files: list[str] | None) -> dict` returning a new doc where each cluster sharing at least one member `comment_id` with a prior cluster carries that prior cluster's `disposition`, `disposition_history` (prior history plus prior latest), and `escalation`; when two prior clusters match, the one with the newest `disposition.at` wins and the others' latest dispositions are appended to history; `disposition.stale = True` when `prior_doc["head"] != new_doc["head"]` and (`changed_files is None` or any `locations[].file` is in `changed_files`); prior clusters with no successor are listed in `new_doc["orphaned_dispositions"]` as `{cluster_id, member_comment_ids, disposition}`; `timeline.plan_at` is carried from `prior_doc`.

- [ ] **Step 1: Write the failing tests**

```python
def test_carries_disposition_by_comment_id_overlap(self): ...       # new c9 shares comment_id 101 with prior c1 -> gets c1's disposition
def test_marks_stale_when_head_moved_and_file_changed(self): ...    # changed_files=["src/a.ts"] -> stale True; ["other"] -> no stale key
def test_unknown_changed_files_marks_everything_stale(self): ...    # changed_files=None
def test_orphaned_prior_dispositions_are_kept(self): ...            # prior c2 has no successor
def test_legacy_prior_doc_carries_nothing(self): ...                # prior is old scored format -> no disposition keys, no orphans
def test_newest_disposition_wins_on_double_match(self): ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_merge -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `scripts/quorum_merge.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.tests.test_merge -v`
Expected: `OK`, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/quorum_merge.py scripts/tests
git commit -m "feat(skill): carry dispositions across re-clustering with stale detection"
```

---

### Task 6: `quorum_render.py` renders the two-layer comment

**Files:**
- Create: `scripts/quorum_render.py`
- Create: `references/triage-protocol.md`
- Create: `scripts/tests/test_render.py`
- Create: `scripts/tests/fixtures/triage.ledger.json`: the Task 3 fixture plus dispositions `c1` valid with `commit: "ba814e4"` and note `both reviewers right`, `c2` valid with `commit: "ba814e4"`, `c3` deferred, `c4` partly with `commit: "ba814e4"`; `c1` also carries an `escalation` with `invariant`, three `siblings`, `chokepoint`, `test` and a `map` of five nodes; `timeline` filled.

**Interfaces:**
- Consumes: `quorum_gate.compute_gate`, `compute_deferred`, `quorum_doc.MARKER`.
- Produces: `MAX_BODY = 60000`; `load_protocol(ledger_ref: str) -> str` (reads `references/triage-protocol.md` relative to the script's parent dir, replaces `{ledger_ref}`); `render_comment(doc: dict, protocol_text: str) -> str` (final body under `MAX_BODY`, applying `shrink_levels`); `render_gate_line(doc) -> str`; `render_cluster_row(cluster, doc) -> list[str]`; `render_invariant_map(cluster) -> str`; `shrink_levels(doc) -> Iterator[dict]` yielding the doc unchanged, then without `members[].url`, then with `escalation.siblings` cut to the first 10 by risk order likely → possible → unlikely, then a minimal doc `{schema, pr, head, totals, lanes, truncated: true, clusters: [{cluster_id, member_ids, quorum, reviewers, lane, disposition}]}` that `parseScoredClusters` in the Node CLI still accepts.

Exact rendering, pinned by the tests:

```
<!-- quorum:synthesis -->
## 🤝 Quorum · triage brief
<sub>head `abc1234` · 3 reviewers · updated 2026-10-29T07:00:00Z</sub>

**⏳ Triage pending** · 2 agreed presumed real · 10 solo queued
**14 findings** from bugbot, devin, greptile → **12 issues**

### Agreed · presumed real, escalated
- **P1** Backlog slice omits the pre-roll · `tools/stt/stt_bench.py:L212-L219` · 2/3 bugbot, devin · [bugbot-1](url), [devin-3](url)
  → escalate · invariant · sibling sweep · fix brief

### Solo · prove or kill
- **P2** Title · `file:L5` · 1/3 devin · [devin-1](url)
  → prove or kill

<details><summary>🤖 For triage agents · protocol, plan and ledger</summary>

{protocol}

```json
{doc}
```
</details>

<sub>Quorum · plan 07:00 · quorum/triage@1</sub>
```

Gate lines: pending as above; blocked `**🛑 Blocked by {k} issue(s)**` + ` · {j} still pending review` when `j > 0` + ` · {titles joined by " · "}`; clear `**✅ Merge gate clear**` + ` · was blocked by {k}` when `k > 0` + ` · fixed in {commits}` + ` · {tests}` when present. Counts line gains ` · {v} valid · {p} partly · {i} dismissed · {d} deferred` once triage has started. Disposition line per row: `✅ valid`, `⚠ partly`, `✖ dismissed` (for `invalid`), `⏸ deferred`, `🔁 re-check` (stale), followed by `` · fixed `sha` `` when `commit`, then ` · {note}`, then ` · no evidence recorded` when the verdict is `valid` or `partly` and `evidence` is empty. Agreed rows with an escalation add `  Invariant: {invariant}` and the invariant map. A `### Deferred by owner · {n}` section lists titles joined by ` · `. Footer shows `plan HH:MM`, `→ validated HH:MM`, `→ fixed HH:MM` from `timeline` when present. Mermaid: `flowchart LR`, node ids sanitised to `[A-Za-z0-9_]`, labels with `"` escaped as `#quot;`, `classDef break stroke:#d73a49,stroke-width:2px`, `classDef fix stroke:#28a745,stroke-width:2px`, `classDef sibling stroke-dasharray:5 5`. Star fallback when `escalation.map` is absent: every `locations[]` (kind source) and `siblings[]` (kind sibling) point at one `fix` node labelled with the first 60 chars of `chokepoint`. More than 12 nodes: a bullet list of labels under `Invariant map:`.

`references/triage-protocol.md` exact text:

```
**Protocol** (for an agent without the Quorum skill)

1. `agreed` clusters: do not re-prove. Establish the missing invariant, sweep for sibling sites, write a fix brief naming the chokepoint and the test. Report with `escalate`.
2. `solo` clusters: prove or kill. Cheapest reproduction first. Return `valid | partly | invalid` with evidence. Report with `dispose`. `priors` in the JSON is each reviewer's past precision here: a prior, never a verdict.
3. If `mechanism_agreement` is `split`, establish the true mechanism before fixing anything.
4. Skip clusters that already carry a disposition unless it is marked `stale: true`.
5. Report with `python3 scripts/quorum_ledger.py dispose OWNER/REPO PR CLUSTER_ID --verdict valid|partly|invalid|deferred --note "..." [--evidence "..."] [--commit SHA] --reply` or `escalate OWNER/REPO PR CLUSTER_ID --from escalation.json`; Quorum re-renders this comment. Get the scripts from a checkout of https://github.com/Wenjix/quorum or `gh api repos/Wenjix/quorum/tarball/{ledger_ref} > quorum.tgz && tar -xzf quorum.tgz` (the `scripts/` and `references/` directories are needed together).
```

- [ ] **Step 1: Write the failing tests**

```python
def test_render_is_deterministic(self): ...                       # two calls, identical string
def test_plan_state_lines(self):
    body = render_comment(load("triage.json"), PROTOCOL)
    self.assertIn("**⏳ Triage pending** · 2 agreed presumed real · 2 solo queued", body)
    self.assertIn("### Agreed · presumed real, escalated", body)
    self.assertIn("  → escalate · invariant · sibling sweep · fix brief", body)
    self.assertLess(body.index("### Agreed"), body.index("### Solo"))
    self.assertIn("tarball/v0.3.0", body)  # protocol embedded with the ledger ref substituted
def test_ledger_state_lines(self):
    body = render_comment(load("triage.ledger.json"), PROTOCOL)
    self.assertIn("**✅ Merge gate clear** · was blocked by 2 · fixed in ba814e4", body)
    self.assertIn("✅ valid · fixed `ba814e4` · both reviewers right", body)
    self.assertIn("```mermaid\nflowchart LR", body)
    self.assertIn("### Deferred by owner · 1", body)
def test_star_fallback_and_bullet_list_over_12_nodes(self): ...
def test_outdated_member_renders_file_only(self): ...              # lines [None, None] -> `file` without :L
def test_valid_without_evidence_is_marked(self): ...               # " · no evidence recorded" present
def test_shrink_levels_keep_body_under_limit(self):
    doc = inflate(load("triage.ledger.json"), note_len=70000)
    body = render_comment(doc, PROTOCOL)
    self.assertLessEqual(len(body), 60000)
    self.assertIn('"truncated": true', body)
def test_json_fence_recoverable(self):
    body = render_comment(load("triage.ledger.json"), PROTOCOL)
    self.assertEqual(extract_doc(body)["schema"], "quorum/triage@1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_render -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Write `references/triage-protocol.md` and implement `scripts/quorum_render.py`**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.tests.test_render -v`
Expected: `OK`, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/quorum_render.py references/triage-protocol.md scripts/tests
git commit -m "feat(skill): render the two-layer triage comment with merge gate and invariant map"
```

---

### Task 7: `quorum_ledger.py` CLI replaces `post_synthesis.py`

**Files:**
- Create: `scripts/quorum_ledger.py`
- Delete: `scripts/post_synthesis.py`
- Create: `scripts/tests/test_ledger.py`
- Create: `scripts/tests/fixtures/escalation.json`, `scripts/tests/fixtures/dispositions.json`

**Interfaces:**
- Consumes: Tasks 3 to 6.
- Produces: `LEDGER_REF = "v0.3.0"`; `main(argv: list[str] | None = None, client: Client | None = None) -> int`. Subcommands:
  - `post-plan OWNER/REPO PR clusters.scored.json [--dry-run] [--minimize] [--no-reactions]`: reads the doc, upgrades legacy input, finds the marker comment, inherits dispositions (fetching `compare_files` when heads differ), sets `timeline.plan_at` if absent, computes `gate`/`deferred`/`timeline`, renders, upserts, adds 👀 to members of `agreed` clusters (unless `--no-reactions`), minimises non-primary members only with `--minimize`. `--dry-run` prints the body and the planned side effects to stderr, no client calls except reads. (Priors come in through `validate_partition.py --priors`; Task 10 adds the default cache lookup here.)
  - `dispose` prints `warning: {cluster_id}: {verdict} recorded without --evidence` to stderr when the verdict is `valid` or `partly` and no evidence was given, and still writes.
  - `dispose OWNER/REPO PR [CLUSTER_ID] --verdict V --note N [--evidence E]... [--member ID:V]... [--severity S] [--commit SHA] [--tests T] [--by NAME] [--reply] [--from FILE]`: `--note` is required for a new verdict; it may be omitted only when the cluster already has a disposition and the call carries only `--commit` and/or `--tests` (the prior verdict and note are kept). Unknown `CLUSTER_ID` exits 2 with `unknown cluster_id: X`. `--from FILE` accepts one object or `{"dispositions": [...]}`; objects may be the validator's JSON block verbatim (`QUORUM_DISPOSITION` key ignored). Each disposition gets `by` (default `"agent"`), `at` (`now_iso()`), previous latest appended to `disposition_history`. `--reply` posts `**Quorum** · {label}{ · fixed in `sha`} · {note}` in every member thread. Re-renders and PATCHes the comment.
  - `escalate OWNER/REPO PR CLUSTER_ID --from FILE`: FILE is the escalation JSON (`QUORUM_ESCALATION` key ignored); stores it under `escalation` with `by`, `at`; also records a disposition `{verdict: escalation.verdict or "valid", note: escalation.invariant, evidence: escalation.evidence}` in the same update. Rejects a `solo` cluster with exit 2.
  - `show OWNER/REPO PR`: prints the current embedded JSON.
- Produces: the doc stores `gate` and `deferred` (recomputed on every write).

- [ ] **Step 1: Write the failing tests** (all through `main(argv, client=FakeClient(...))`)

```python
def test_post_plan_posts_new_comment_and_reacts(self): ...         # POST comment; eyes on agreed members only
def test_post_plan_dry_run_makes_no_writes(self): ...
def test_post_plan_rerun_patches_and_carries_disposition(self): ... # prior comment body has a disposition on overlapping comment_id
def test_post_plan_over_legacy_comment_upgrades_in_place(self): ...
def test_dispose_requires_note_for_new_verdict(self): ...           # exit 2, no PATCH
def test_dispose_unknown_cluster(self): ...                         # exit 2, stderr names id
def test_dispose_commit_only_keeps_prior_verdict(self): ...
def test_dispose_from_file_and_reply(self): ...                     # two dispositions; replies POSTed per member
def test_escalate_writes_escalation_and_valid_disposition(self): ...
def test_escalate_rejects_solo_cluster(self): ...
def test_show_prints_embedded_json(self): ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_ledger -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `scripts/quorum_ledger.py`; delete `scripts/post_synthesis.py`**

`argparse` with subparsers. Side effects are best-effort as before (a failed reaction or reply prints a count, never aborts).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest scripts.tests.test_ledger -v`
Expected: `OK`, 11 tests.

- [ ] **Step 5: Smoke the whole skill path offline on the fixtures**

Run: `python3 scripts/validate_partition.py scripts/tests/fixtures/findings.json scripts/tests/fixtures/clusters.json -o /tmp/q.scored.json && python3 scripts/quorum_ledger.py post-plan o/r 7 /tmp/q.scored.json --dry-run 2>/dev/null | head -20`
Expected: the body starts with the marker and the `## 🤝 Quorum · triage brief` heading.

- [ ] **Step 6: Commit**

```bash
git add scripts/quorum_ledger.py scripts/tests
git rm scripts/post_synthesis.py
git commit -m "feat(skill): quorum_ledger.py owns plan, dispose, escalate and show"
```

---

### Task 8: Skill instructions, validator and escalation prompts

**Files:**
- Modify: `SKILL.md`
- Create: `references/validator-prompt.md`
- Create: `references/escalation-prompt.md`

**Interfaces:**
- Consumes: every script interface above.
- Produces: `references/validator-prompt.md` tells a read-only subagent to prove or kill up to four `solo` clusters (given as JSON) with the cheapest reproduction first and to end with one fenced JSON block per cluster: `{"QUORUM_DISPOSITION": true, "cluster_id", "verdict": "valid|partly|invalid", "note": "one sentence", "evidence": ["path:L1-L2 what was observed"], "members": {"id": "verdict"}?, "severity"?: "critical|major|minor|nit", "mechanism_correction"?: "..."}`.
- Produces: `references/escalation-prompt.md` tells a read-only subagent, for one `agreed` cluster, to presume the finding real, establish the missing invariant, sweep the repository for sibling sites, name the chokepoint and the test, and end with one fenced JSON block: `{"QUORUM_ESCALATION": true, "cluster_id", "verdict": "valid"|"partly"|"invalid", "invariant", "siblings": [{"file","lines","risk": "likely|possible|unlikely","rationale"}], "chokepoint", "test", "evidence": [{"file","lines","note"}], "map"?: {"nodes": [{"id","label","kind": "source|break|symptom|fix|sibling"}], "edges": [["from","to"]]}}`. It reuses the root-cause and pattern-sweep wording from `src/prompts.ts`. A verdict other than `valid` requires evidence that disproves the premise.

- [ ] **Step 1: Load the `writing-for-agents` skill, then rewrite `SKILL.md`**

Keep the frontmatter name; update the description to say triage director, lanes, one ledger comment. Sections, in order: core rules (the existing four plus "every comment write goes through `scripts/quorum_ledger.py`" and "agreed clusters are presumed real: escalate, never re-prove"); preconditions; Step 0 fetch and count reviewers (`scripts/fetch_findings.sh`; one reviewer → Step 2 with `--all-singletons`, no rubric read; zero → the existing guidance); Step 1 cluster (read the rubric, including the two new fields); Step 2 validate and score (`validate_partition.py ... [--priors .quorum/priors/<owner>-<repo>.json]`); Step 3 post the plan (`quorum_ledger.py post-plan ... --dry-run`, show it, then post; non-interactive posts directly; `--no-comment` means skip this step); Step 4 offer to drive the triage (one escalation subagent per `agreed` cluster with `references/escalation-prompt.md`, validator subagents in batches of four `solo` clusters with `references/validator-prompt.md`, all read-only, results saved as files and reported with `escalate --from` and `dispose --from --reply`); Step 5 offer fixes, then `dispose CLUSTER --commit SHA --tests "..." --reply` per fixed cluster; Step 6 report and append `{pr, findings, clusters_scored, triage}` (the last from `show`) to `.quorum/log/<date>-pr<N>.json`; Phase 2 shortcuts (unchanged commands; note that `post-pr` still writes its own exploration comment until the runner follow-up); known simplifications (drop the resolved ones, keep the denominator note).

- [ ] **Step 2: Write the two prompt references**

- [ ] **Step 3: Verify the skill packages and references resolve**

Run: `npm run build:skill && unzip -l quorum.skill | grep -E "triage-protocol|validator-prompt|escalation-prompt|quorum_ledger|post_synthesis|tests/"`
Expected: the three references and `quorum_ledger.py` listed; no `post_synthesis.py`; no `tests/` entries.

- [ ] **Step 4: Commit**

```bash
git add SKILL.md references/validator-prompt.md references/escalation-prompt.md quorum.skill
git commit -m "feat(skill): triage-director instructions with validator and escalation prompts"
```

---

### Task 9: Node CLI reads `quorum/triage@1` and shells out to the ledger

**Files:**
- Modify: `src/types.ts`
- Modify: `src/clusters.ts`
- Modify: `src/cli.ts:132-138` (selection), `src/cli.ts:397-413` (synthesize post), `src/cli.ts:418-438` (`generateSingletonClusters`), `src/cli.ts:738-790` (help)
- Modify: `test/helpers.ts`, `test/clusters.test.ts`, `test/synthesis.test.ts`

**Interfaces:**
- Produces `src/types.ts`: `ScoredCluster` gains optional `lane?: "agreed" | "solo" | string`, `p_tag?: string`, `disposition?: unknown`, `escalation?: unknown`; `ScoredClustersDoc` gains optional `schema?: string`, `pr?: string`, `head?: string`, `lanes?: { agreed: string[]; solo: string[] }`.
- Produces `src/clusters.ts`: `selectClusters(doc, options)` where `options.minQuorum` undefined and any cluster has `lane` → select `lane === "agreed"`; `minQuorum` undefined and no lanes → `quorum >= 2`; `minQuorum` given → quorum floor as today.
- Produces `src/cli.ts`: `executeExplore` passes `minQuorum: optionalNumberFlag(parsed, "min-quorum")`; `synthesizeCommand` runs `python3 scripts/quorum_ledger.py post-plan REPO PR SCORED [--dry-run] [--minimize] [--no-reactions]`; `generateSingletonClusters` uses `finding.stated_severity ?? "minor"`; help text: `--min-quorum N   Quorum floor; default selects the agreed lane (quorum >= 2).`
- Produces `test/helpers.ts`: `sampleTriageDoc(): ScoredClustersDoc` (schema, lanes, `lane` on each cluster).

- [ ] **Step 1: Write the failing tests**

```ts
test("selectClusters uses the agreed lane when lanes are present", () => {
  const selected = selectClusters(sampleTriageDoc());
  assert.deepEqual(selected.map((c) => c.cluster_id), ["c1"]);
});
test("selectClusters honours an explicit --min-quorum over lanes", () => {
  const selected = selectClusters(sampleTriageDoc(), { minQuorum: 1 });
  assert.equal(selected.length, 2);
});
test("extractScoredClustersFromCommentBody recovers quorum/triage@1 from a ledger comment", () => {
  const body = ["<!-- quorum:synthesis -->", "## 🤝 Quorum · triage brief", "```mermaid", "flowchart LR", "```",
    "<details>", "```json", JSON.stringify(sampleTriageDoc()), "```", "</details>"].join("\n");
  const doc = extractScoredClustersFromCommentBody(body);
  assert.equal(doc.schema, "quorum/triage@1");
  assert.equal(doc.clusters[0].lane, "agreed");
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npm run build && node --test dist/test/clusters.test.js dist/test/synthesis.test.js`
Expected: FAIL on `sampleTriageDoc` missing and lane selection.

- [ ] **Step 3: Implement the type, selection, CLI and help changes**

- [ ] **Step 4: Run the full suite**

Run: `npm run typecheck && npm test`
Expected: PASS, node and python.

- [ ] **Step 5: Commit**

```bash
git add src/types.ts src/clusters.ts src/cli.ts test/helpers.ts test/clusters.test.ts test/synthesis.test.ts
git commit -m "feat(cli): read quorum/triage@1, select by lane, post through quorum_ledger.py"
```

---

### Task 10: Priors from past ledgers

**Files:**
- Create: `scripts/quorum_priors.py`
- Create: `scripts/tests/test_priors.py`
- Modify: `src/cli.ts` (new `priors` command and help line)

**Interfaces:**
- Consumes: `quorum_github` (`Client`, pagination via `params={"paginate": True}`), `quorum_doc.extract_doc`, `quorum_doc.latest_disposition`.
- Produces: `MIN_N = 5`, `MAX_AGE_DAYS = 7`; `aggregate(docs: Iterable[dict]) -> dict` returning `{reviewer: {"valid": f, "partly": f, "invalid": f, "n": int}}` from member-level verdicts (`disposition.members.get(member_id, disposition.verdict)`, `deferred` ignored), reviewers with `n < MIN_N` omitted; `collect_docs(client, repo) -> list[dict]` (all PRs `state=all`, marker comments, parsed docs); `main(argv=None, client=None) -> int` writing `{"generated_at", "repo", "reviewers": ...}` to `.quorum/priors/<owner>-<repo>.json` (or `--out PATH`); `load_fresh_priors(path: str, now: datetime) -> dict` returning `reviewers` when the file is younger than `MAX_AGE_DAYS`, else `{}` (used by `quorum_ledger.py post-plan`).
- Produces `quorum priors OWNER/REPO [--out PATH]` in `src/cli.ts`, shelling out to the script.

- [ ] **Step 1: Write the failing tests**

```python
def test_aggregate_uses_member_overrides(self): ...        # cluster verdict valid, member devin-3 partly -> devin partly count 1
def test_reviewers_under_min_n_are_omitted(self): ...
def test_deferred_is_ignored(self): ...
def test_load_fresh_priors_respects_age(self): ...         # 8 days old -> {}
def test_collect_docs_reads_marker_comments_across_prs(self): ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest scripts.tests.test_priors -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Implement `scripts/quorum_priors.py`, wire `load_fresh_priors` into `post-plan`, add the CLI command**

- [ ] **Step 4: Run the full suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/quorum_priors.py scripts/quorum_ledger.py scripts/tests src/cli.ts
git commit -m "feat: per-bot priors computed from past ledgers"
```

---

### Task 11: Docs, version 0.3.0 and the release tag

**Files:**
- Modify: `README.md`, `AGENTS.md`, `package.json`, `quorum.skill`

- [ ] **Step 1: Update the docs**

`README.md`: "What Quorum Does" describes lanes and the one comment; "Use the Quorum Skill" shows the new step list and the `--no-comment` fast path; "Outputs" documents the three comment states and the `quorum/triage@1` block; CLI section adds `quorum priors` and the new `--min-quorum` meaning; remove every `post_synthesis.py` mention; note that `post-pr` still writes the exploration comment until the runner follow-up. `AGENTS.md`: `npm test` now includes the python suite under `scripts/tests`; the ledger script is the only comment writer.

- [ ] **Step 2: Bump the version and rebuild the skill**

`package.json` version `0.3.0`; confirm `LEDGER_REF = "v0.3.0"` in `scripts/quorum_ledger.py`. Run `npm run build:skill`.

- [ ] **Step 3: Verify**

Run: `npm run typecheck && npm test && npm run build:skill && git status --short`
Expected: all PASS; only the intended files modified.

- [ ] **Step 4: Commit and tag locally**

```bash
git add README.md AGENTS.md package.json package-lock.json quorum.skill
git commit -m "chore: release v0.3.0 (triage director)"
git tag -a v0.3.0 -m "Quorum 0.3.0: triage director"
```

- [ ] **Step 5: Owner steps, not for the implementer**

Push the branch and the tag (`git push -u origin feat/triage-director && git push origin v0.3.0`), open the PR, and reinstall the local skill copy with `unzip -o quorum.skill -d ~/.claude/skills` so it matches the repo. The protocol's tarball fetch path only works once the tag is on GitHub.

---

## Self-review notes

- Spec coverage: 6.1 to 6.12, 7, 9 and 12 map to Tasks 1 to 11. 6.13 option B is Task 9 plus the `post-pr` note in Task 8 and Task 11. Section 10 items are untouched by design.
- One deviation from the spec text, for a technical reason: spec 6.7 line 5 fetches a single file via the contents API, but the ledger imports sibling modules and the protocol reference, so the protocol in Task 6 fetches the repo tarball at the tag instead. The spec is updated in the same commit as this plan.
- Names used across tasks: `extract_doc`, `upgrade_legacy`, `latest_disposition`, `compute_gate`, `compute_deferred`, `compute_timeline`, `inherit`, `render_comment`, `load_protocol`, `shrink_levels`, `FakeClient`, `LEDGER_REF`, `sampleTriageDoc`. Each is defined in exactly one task before use.
