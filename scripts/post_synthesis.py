#!/usr/bin/env python3
"""post_synthesis.py — render and post the Quorum synthesis comment.

- Upserts ONE issue comment on the PR, idempotent via a hidden HTML marker:
  re-runs update the same comment in place, never spam.
- Adds eyes reactions to the original bot comments belonging to quorum >= 2
  clusters (the in-thread cue that another reviewer agrees).
- Embeds the scored clusters JSON in a collapsed <details> block — the
  machine-readable surface for downstream agents. Big PRs degrade gracefully:
  pretty JSON -> compact JSON -> slimmed payload -> omitted with a note, so
  the machine surface survives on exactly the PRs where agents need it most.
- After posting, fetches the comment back and verifies the marker landed.
- --minimize collapses non-primary members of multi-finding clusters as
  DUPLICATE (hides other bots' comments: opt-in, ask the user first).
- --dry-run renders to stdout and lists planned side effects, no gh calls.

usage: post_synthesis.py OWNER/REPO PR_NUMBER clusters.scored.json
       [--dry-run] [--minimize] [--no-reactions]
"""

import argparse
import json
import subprocess
import sys

MARKER = "<!-- quorum:synthesis -->"
MAX_BODY = 60000  # GitHub comment hard limit is 65536

TIER_EMOJI_FULL = "\U0001F3AF"   # direct hit
TIER_EMOJI_MULTI = "\u26A1"      # high voltage
TIER_EMOJI_SOLO = "\u25FD"       # small square


def gh(*args, payload=None):
    """Run a gh command; returns stdout. Raises CalledProcessError on failure."""
    res = subprocess.run(
        ["gh", *args],
        check=True, capture_output=True, text=True,
        input=payload,
    )
    return res.stdout


def tier_emoji(quorum, denominator):
    if quorum >= 2 and quorum == denominator:
        return TIER_EMOJI_FULL
    if quorum >= 2:
        return TIER_EMOJI_MULTI
    return TIER_EMOJI_SOLO


def loc_str(c):
    loc = c.get("primary_location") or {}
    f = loc.get("file") or "?"
    a, b = loc.get("start_line"), loc.get("end_line")
    if a and b and a != b:
        return f"`{f}:L{a}-L{b}`"
    if a or b:
        return f"`{f}:L{a or b}`"
    return f"`{f}`"


def slim_payload(data):
    """Reduced embed for very large PRs: keeps ids, titles, quorum, and links —
    enough for downstream agents to act — and drops the long descriptions and
    per-member metadata that dominate the byte count."""
    keep = ("cluster_id", "member_ids", "canonical_title", "category", "severity",
            "match_type", "match_confidence", "single_fix", "quorum", "reviewers",
            "gate_split_from")
    return {
        "generated_at": data.get("generated_at"),
        "totals": data["totals"],
        "slim": True,
        "clusters": [
            {**{k: c[k] for k in keep if k in c},
             "members": [{"id": m["id"], "url": m["url"],
                          "comment_id": m.get("comment_id")} for m in c["members"]]}
            for c in data["clusters"]
        ],
    }


def render(repo, pr, data, json_mode="full"):
    totals = data["totals"]
    d = max(totals["reviewer_denominator"], 1)
    clusters = data["clusters"]
    reviewers = sorted({r for c in clusters for r in c["reviewers"]})

    out = [MARKER, "## \U0001F91D Quorum — review synthesis", ""]
    out.append(
        f"**{totals['findings']} findings** from {', '.join(reviewers) or 'no reviewers'} "
        f"\u2192 **{totals['clusters']} distinct issues**. "
        f"Sorted by reviewer quorum, then severity."
    )
    out.append("")

    current_q = None
    for c in clusters:
        q = c["quorum"]
        if q != current_q:
            current_q = q
            label = "reviewers agree" if q > 1 else "reviewer"
            if out[-1] != "":
                out.append("")
            out.append(f"### {tier_emoji(q, d)} {q}/{d} {label}")
            out.append("")
        members = ", ".join(f"[{m['id']}]({m['url']})" for m in c["members"])
        out.append(
            f"- **[{c['severity']}]** {c['canonical_title']} — {loc_str(c)} · "
            f"{c['category']} · {members}"
        )
        desc = (c.get("canonical_description") or "").strip()
        if desc and desc != c["canonical_title"]:
            out.append(f"  {desc}")
        if len(c.get("member_ids", [])) > 1 and (c.get("single_fix") or "").strip():
            out.append(f"  <sub>single fix: {c['single_fix'].strip()}</sub>")
        if c.get("gate_split_from"):
            out.append(f"  <sub>split from low-confidence merge `{c['gate_split_from']}`</sub>")
    out.append("")

    if totals.get("gate_split"):
        out.append(
            f"<sub>Confidence gate split {len(totals['gate_split'])} proposed merge(s) "
            f"back into singletons \u2014 quorum is shown only where the match is confident.</sub>"
        )
        out.append("")

    if json_mode != "none":
        payload = slim_payload(data) if json_mode == "slim" else data
        dumped = (json.dumps(payload, indent=2) if json_mode == "full"
                  else json.dumps(payload, separators=(",", ":")))
        out.append("<details><summary>clusters.scored.json (machine-readable, for agents)</summary>")
        out.append("")
        out.append("```json")
        out.append(dumped)
        out.append("```")
        out.append("</details>")
        out.append("")
        if json_mode == "slim":
            out.append("<sub>Embedded JSON slimmed to fit the comment size limit "
                       "(descriptions and per-member metadata trimmed); the full "
                       "clusters.scored.json is in the local run artifacts.</sub>")
            out.append("")
    else:
        out.append("<sub>JSON payload omitted: comment size limit. "
                   "See the local run artifacts for clusters.scored.json.</sub>")
        out.append("")

    out.append(f"<sub>Quorum · PR {repo}#{pr} · generated {data.get('generated_at', '')}</sub>")
    return "\n".join(out)


def find_existing_comment(repo, pr):
    raw = gh(
        "api", f"repos/{repo}/issues/{pr}/comments", "--paginate",
        "--jq", '.[] | {id: .id, hit: (.body | contains("quorum:synthesis"))} | @json',
    )
    for line in raw.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("hit"):
            return obj["id"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", help="OWNER/REPO")
    ap.add_argument("pr", help="PR number")
    ap.add_argument("scored", help="clusters.scored.json from validate_partition.py")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--minimize", action="store_true")
    ap.add_argument("--no-reactions", action="store_true")
    args = ap.parse_args()

    with open(args.scored) as f:
        data = json.load(f)
    clusters = data["clusters"]

    # Degrade the embedded JSON gradually instead of dropping it outright:
    # downstream agents need the machine surface most on exactly the big PRs
    # that hit the size limit.
    for json_mode in ("full", "compact", "slim", "none"):
        body = render(args.repo, args.pr, data, json_mode=json_mode)
        if len(body) <= MAX_BODY:
            break
    if json_mode != "full":
        print(f"comment size limit: embedded JSON degraded to '{json_mode}'",
              file=sys.stderr)

    react_targets = [
        m for c in clusters if c["quorum"] >= 2 for m in c["members"]
    ]
    minimize_targets = [
        m for c in clusters if len(c["members"]) > 1 for m in c["members"][1:]
    ]

    if args.dry_run:
        print(body)
        print("\n--- planned side effects (dry run) ---", file=sys.stderr)
        print(f"upsert synthesis comment on {args.repo}#{args.pr}", file=sys.stderr)
        if not args.no_reactions:
            print(f"eyes reactions on {len(react_targets)} comment(s): "
                  f"{[m['id'] for m in react_targets]}", file=sys.stderr)
        if args.minimize:
            print(f"minimize as DUPLICATE: {[m['id'] for m in minimize_targets]}", file=sys.stderr)
        return

    # 1. Upsert synthesis comment
    payload = json.dumps({"body": body})
    existing = find_existing_comment(args.repo, args.pr)
    if existing:
        gh("api", "-X", "PATCH", f"repos/{args.repo}/issues/comments/{existing}",
           "--input", "-", payload=payload)
        comment_id = existing
        print(f"updated synthesis comment {comment_id}")
    else:
        resp = gh("api", "-X", "POST", f"repos/{args.repo}/issues/{args.pr}/comments",
                  "--input", "-", payload=payload)
        try:
            comment_id = json.loads(resp).get("id")
        except json.JSONDecodeError:
            comment_id = None
        print(f"posted new synthesis comment {comment_id or ''}".rstrip())

    # 1b. Verify the write actually landed (fetch it back, check the marker)
    if comment_id:
        try:
            posted = gh("api", f"repos/{args.repo}/issues/comments/{comment_id}",
                        "--jq", ".body")
            if MARKER in posted:
                print(f"verified: comment {comment_id} carries the quorum marker")
            else:
                print(f"warning: comment {comment_id} is missing the quorum marker — "
                      "future runs would post a duplicate; inspect the comment",
                      file=sys.stderr)
        except subprocess.CalledProcessError:
            print(f"warning: could not re-fetch comment {comment_id} to verify",
                  file=sys.stderr)
    else:
        print("warning: no comment id returned; skipping verification", file=sys.stderr)

    # 2. Reactions on quorum >= 2 members (best-effort)
    if not args.no_reactions:
        ok = 0
        for m in react_targets:
            try:
                gh("api", "-X", "POST",
                   f"repos/{args.repo}/pulls/comments/{m['comment_id']}/reactions",
                   "-f", "content=eyes")
                ok += 1
            except subprocess.CalledProcessError:
                pass
        print(f"reactions: {ok}/{len(react_targets)}")

    # 3. Optional minimize of duplicate members (best-effort)
    if args.minimize:
        mutation = (
            "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, "
            "classifier: DUPLICATE}) { minimizedComment { isMinimized } } }"
        )
        ok = 0
        for m in minimize_targets:
            if not m.get("node_id"):
                continue
            try:
                gh("api", "graphql", "-f", f"query={mutation}", "-f", f"id={m['node_id']}")
                ok += 1
            except subprocess.CalledProcessError:
                pass
        print(f"minimized: {ok}/{len(minimize_targets)}")


if __name__ == "__main__":
    main()
