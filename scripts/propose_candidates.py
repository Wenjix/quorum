#!/usr/bin/env python3
"""propose_candidates.py — deterministic candidate-pair blocking for the clustering judge.

Why this exists: one-shot clustering over a large findings set degrades — the
judge's attention spreads across O(n^2) implicit pairs and merge precision
drops exactly where the product's signal lives. The classic entity-resolution
fix is blocking: generate candidate pairs with cheap, high-recall,
deterministic signals, then let the judge spend its attention only on those
pairs. Findings that share no candidate pair default to separate clusters
unless the judge has explicit evidence otherwise.

Signals per pair (all deterministic, no model calls):
  - same file with overlapping or nearby line ranges (gap <= --line-gap)
  - token Jaccard similarity over comment bodies (>= --min-sim)
  - cross-file pairs must clear a higher similarity bar (>= --cross-file-sim),
    mirroring the rubric's stricter rule for cross-file merges

usage: propose_candidates.py findings.json [-o candidates.json]
       [--line-gap 40] [--min-sim 0.25] [--cross-file-sim 0.4] [--max-pairs 80]
"""

import argparse
import json
import re
import sys

STOPWORDS = {
    "the", "and", "this", "that", "with", "for", "not", "can", "will",
    "should", "would", "could", "may", "might", "here", "when", "where",
    "which", "are", "was", "has", "have", "been", "its", "but", "you",
    "your", "from", "into", "also", "than", "then", "there",
}

TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")


def tokens(text):
    return {t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOPWORDS}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def line_gap(f1, f2):
    """Minimum gap between the two findings' line ranges; 0 if they overlap."""
    l1 = [x for x in (f1.get("lines") or []) if isinstance(x, int)]
    l2 = [x for x in (f2.get("lines") or []) if isinstance(x, int)]
    if not l1 or not l2:
        return None
    a1, b1 = min(l1), max(l1)
    a2, b2 = min(l2), max(l2)
    if b1 < a2:
        return a2 - b1
    if b2 < a1:
        return a1 - b2
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("findings")
    ap.add_argument("-o", "--out", default="candidates.json")
    ap.add_argument("--line-gap", type=int, default=40)
    ap.add_argument("--min-sim", type=float, default=0.25)
    ap.add_argument("--cross-file-sim", type=float, default=0.4)
    ap.add_argument("--max-pairs", type=int, default=80)
    args = ap.parse_args()

    with open(args.findings) as f:
        findings = json.load(f)

    toks = {f["id"]: tokens(f.get("body")) for f in findings}
    pairs = []
    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            f1, f2 = findings[i], findings[j]
            same_file = bool(f1.get("file")) and f1.get("file") == f2.get("file")
            gap = line_gap(f1, f2) if same_file else None
            sim = jaccard(toks[f1["id"]], toks[f2["id"]])

            reasons = []
            if same_file and gap is not None and gap <= args.line_gap:
                reasons.append(
                    "same file, overlapping lines" if gap == 0
                    else f"same file, line gap {gap}"
                )
            if same_file and sim >= args.min_sim:
                reasons.append(f"body similarity {sim:.2f}")
            if not same_file and sim >= args.cross_file_sim:
                reasons.append(f"cross-file body similarity {sim:.2f}")

            if reasons:
                pairs.append({
                    "a": f1["id"],
                    "b": f2["id"],
                    "same_file": same_file,
                    "line_gap": gap,
                    "token_jaccard": round(sim, 3),
                    "cross_file": not same_file,
                    "reasons": reasons,
                })

    pairs.sort(key=lambda p: (-int(p["same_file"]), -p["token_jaccard"]))
    truncated = len(pairs) > args.max_pairs
    pairs = pairs[: args.max_pairs]

    result = {
        "findings": len(findings),
        "candidate_pairs": len(pairs),
        "truncated": truncated,
        "note": (
            "Candidate pairs only — high recall, low precision by design. "
            "Judge each pair with the single-fix test from the rubric; a pair "
            "listed here is NOT presumed to match. Findings absent from every "
            "pair default to singleton clusters."
        ),
        "pairs": pairs,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(f"{len(findings)} findings -> {len(pairs)} candidate pair(s)"
          f"{' (truncated)' if truncated else ''} -> {args.out}")
    if not pairs:
        print("no candidate pairs: all findings default to singletons", file=sys.stderr)


if __name__ == "__main__":
    main()
