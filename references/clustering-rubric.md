# Clustering rubric

You are deduplicating findings from automated code reviewers (Bugbot, Copilot, Devin, …) on a single pull request. Partition **all** findings into clusters, where each cluster represents exactly one underlying issue. You are the judge; read this file in full, then write `clusters.json`.

Two role boundaries, non-negotiable:

- **You match; you do not re-review.** Never drop, merge away, or down-rank a finding because you think it's wrong. Validity gets measured later, from fix data.
- **You do not compute quorum.** That happens deterministically in `validate_partition.py`.

## The test

Two findings belong in the same cluster **if and only if a single code change would plausibly resolve both.** Same root cause, same fix → same cluster.

### MERGE when findings describe the same defect even if they are:

- anchored to different lines — one flags where the bug originates, another where it manifests downstream
- worded very differently, or given different severities by their reviewers
- at different abstraction levels: one general ("no error handling in this function"), one a specific instance ("this await can reject unhandled") → merge with `match_type: "general-specific"`
- from the **same** reviewer — bots duplicate themselves across re-review passes → `match_type: "within-reviewer-dup"`

### DO NOT MERGE when:

- findings are the same *category* of bug at independent locations requiring independent fixes — two separate missing null checks are two clusters
- different issues happen to anchor on the same line
- one is functional and the other purely stylistic, even if co-located
- **you are uncertain.** A wrong merge fabricates reviewer consensus, which is worse than leaving a duplicate. Only merge when the single-fix test clearly passes.

Cross-file merges are allowed **only** when the root cause is literally shared (e.g. both findings trace to the same mutated shared constant). Set `"cross_file": true` and justify in `match_rationale`.

Reviewer identity is irrelevant to whether two findings match. Use the `hunk` diff context to judge — comment prose alone is often too vague to tell whether two descriptions point at the same defect.

## Output schema

Write strict JSON to `clusters.json`. Hard constraints:

- Every input finding `id` appears in **exactly one** cluster. No omissions, no duplicates, no invented ids.
- Singleton clusters are expected and fine — most clusters will be singletons.
- `canonical_title`: ≤ 80 chars, names the defect, not the symptom.
- `canonical_description`: 1–3 sentences synthesizing the **union** of information across members — if one reviewer adds detail the others missed, keep it.
- `category`: one of `logic | concurrency | security | performance | error-handling | data-integrity | api-contract | style | docs | test-gap | other`
- `severity`: max across members, one of `critical | major | minor | nit`
- `match_type`: `exact | same-root-cause | general-specific | within-reviewer-dup | singleton`
- `match_confidence`: 0.0–1.0; use 1.0 for singletons. Multi-finding clusters below 0.7 will be split back into singletons by the validator — that gate is intentional, do not inflate confidence to dodge it.
- `match_rationale`: one sentence, required for clusters of size > 1.

```json
{
  "clusters": [
    {
      "cluster_id": "c1",
      "member_ids": ["bugbot-3", "devin-1"],
      "canonical_title": "string",
      "canonical_description": "string",
      "category": "logic",
      "severity": "major",
      "primary_location": {"file": "string", "start_line": 0, "end_line": 0},
      "match_type": "same-root-cause",
      "match_confidence": 0.9,
      "match_rationale": "one sentence; required for size > 1",
      "cross_file": false
    }
  ]
}
```

`primary_location` is where a human should look first — usually the origin of the defect, not a downstream symptom.

## Worked examples

**A — merge across different lines (same-root-cause):**

- `bugbot-2` @ `utils/options.ts:14` — "Object.assign(DEFAULT_OPTIONS, userOpts) mutates the shared default object; later callers inherit this user's prefs."
- `devin-4` @ `routes/trip.ts:88` — "Route preferences appear to leak between requests; defaults polluted by prior calls."

→ One fix (clone before assign) resolves both. **MERGE**, primary_location at the mutation site.

**B — do not merge (same category, independent instances):**

- `copilot-1` @ `api/users.ts:42` — missing null check on `req.user`
- `bugbot-5` @ `api/orders.ts:17` — missing null check on `order.customer`

→ Independent fixes. **TWO clusters.**

**C — merge general + specific:**

- `devin-2` @ `services/sync.ts` (function-level) — "No error handling in syncAll; any failure leaves partial state."
- `bugbot-7` @ `services/sync.ts:103` — "await push() can reject and is unhandled."

→ The specific is an instance of the general. **MERGE**, `match_type: "general-specific"`; the description covers the broad gap and cites line 103 as a concrete instance.

<!-- Add new worked examples here from dogfood logs: every bad merge you catch in the wild is the next example. -->
