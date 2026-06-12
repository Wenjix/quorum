#!/usr/bin/env python3
"""validate_partition.py — the deterministic harness around the clustering judge.

1. Validates clusters.json against findings.json:
     - member_ids form an EXACT partition of finding ids
       (no omissions, no duplicates, no invented ids)
     - schema sanity: required fields, enum values, confidence in [0,1],
       rationale present on multi-finding clusters
   On violation: prints each named violation and exits 1 so the agent can fix
   clusters.json and retry.

2. Applies the confidence gate: multi-finding clusters with
   match_confidence < threshold (default 0.7) are split back into singletons.
   A false merge fabricates reviewer consensus; a missed merge is just noise.

3. Computes quorum per cluster = count of DISTINCT reviewers among members
   (within-reviewer dups collapse without inflating consensus), sorts by
   (quorum desc, severity desc), and writes clusters.scored.json.

usage: validate_partition.py findings.json clusters.json [-o clusters.scored.json]
       [--confidence-gate 0.7] [--no-gate]
"""

import argparse
import datetime
import json
import sys

SEVERITIES = {"critical": 3, "major": 2, "minor": 1, "nit": 0}
CATEGORIES = {
    "logic", "concurrency", "security", "performance", "error-handling",
    "data-integrity", "api-contract", "style", "docs", "test-gap", "other",
}
MATCH_TYPES = {
    "exact", "same-root-cause", "general-specific",
    "within-reviewer-dup", "singleton",
}


def load(path, label):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: could not read {label} from {path}: {e}", file=sys.stderr)
        sys.exit(1)


def first_line(text, limit=80):
    for line in (text or "").strip().splitlines():
        line = line.strip().lstrip("#*-> ").strip()
        if line:
            return line[:limit]
    return "(no comment text)"


def singleton_from(finding, parent_id=None):
    lines = finding.get("lines") or [None, None]
    c = {
        "cluster_id": f"{finding['id']}-solo",
        "member_ids": [finding["id"]],
        "canonical_title": first_line(finding.get("body")),
        "canonical_description": first_line(finding.get("body"), 240),
        "category": "other",
        "severity": "minor",
        "primary_location": {
            "file": finding.get("file"),
            "start_line": lines[0],
            "end_line": lines[1],
        },
        "match_type": "singleton",
        "match_confidence": 1.0,
        "cross_file": False,
    }
    if parent_id:
        c["gate_split_from"] = parent_id
    return c


def validate(findings, doc):
    errors = []
    if not isinstance(doc, dict) or not isinstance(doc.get("clusters"), list):
        return ['clusters.json must be an object with a "clusters" array'], []

    clusters = doc["clusters"]
    fmap = {f["id"]: f for f in findings}
    seen = {}

    for i, c in enumerate(clusters):
        cid = c.get("cluster_id") or f"<cluster at index {i}>"
        mids = c.get("member_ids") or []
        if not mids:
            errors.append(f"{cid}: empty member_ids")
        for m in mids:
            if m not in fmap:
                errors.append(f"{cid}: invented id '{m}' (not in findings.json)")
            elif m in seen:
                errors.append(f"finding '{m}' appears in both '{seen[m]}' and '{cid}'")
            else:
                seen[m] = cid

        for field in ("cluster_id", "canonical_title", "category", "severity", "match_type"):
            if not c.get(field):
                errors.append(f"{cid}: missing required field '{field}'")
        if c.get("severity") and c["severity"] not in SEVERITIES:
            errors.append(f"{cid}: severity '{c['severity']}' not in {sorted(SEVERITIES)}")
        if c.get("category") and c["category"] not in CATEGORIES:
            errors.append(f"{cid}: category '{c['category']}' not in {sorted(CATEGORIES)}")
        if c.get("match_type") and c["match_type"] not in MATCH_TYPES:
            errors.append(f"{cid}: match_type '{c['match_type']}' not in {sorted(MATCH_TYPES)}")

        conf = c.get("match_confidence")
        if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
            errors.append(f"{cid}: match_confidence must be a number in [0,1], got {conf!r}")
        if len(mids) > 1 and not (c.get("match_rationale") or "").strip():
            errors.append(f"{cid}: match_rationale is required for clusters of size > 1")

    omitted = [fid for fid in fmap if fid not in seen]
    for fid in omitted:
        errors.append(f"finding '{fid}' omitted from all clusters")

    return errors, clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("findings")
    ap.add_argument("clusters")
    ap.add_argument("-o", "--out", default="clusters.scored.json")
    ap.add_argument("--confidence-gate", type=float, default=0.7)
    ap.add_argument("--no-gate", action="store_true")
    args = ap.parse_args()

    findings = load(args.findings, "findings")
    doc = load(args.clusters, "clusters")
    fmap = {f["id"]: f for f in findings}

    errors, clusters = validate(findings, doc)
    if errors:
        print("VALIDATION FAILED — fix these in clusters.json and re-run:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Confidence gate
    gated, out_clusters = [], []
    for c in clusters:
        if (
            not args.no_gate
            and len(c["member_ids"]) > 1
            and float(c["match_confidence"]) < args.confidence_gate
        ):
            gated.append(c["cluster_id"])
            out_clusters.extend(singleton_from(fmap[m], c["cluster_id"]) for m in c["member_ids"])
        else:
            out_clusters.append(dict(c))

    # Deterministic quorum + enrichment
    for c in out_clusters:
        reviewers = sorted({fmap[m]["reviewer"] for m in c["member_ids"]})
        c["quorum"] = len(reviewers)
        c["reviewers"] = reviewers
        c["members"] = [
            {k: fmap[m].get(k) for k in
             ("id", "reviewer", "file", "lines", "url", "comment_id", "node_id", "outdated")}
            for m in c["member_ids"]
        ]

    denominator = len({f["reviewer"] for f in findings})
    out_clusters.sort(key=lambda c: (
        -c["quorum"],
        -SEVERITIES[c["severity"]],
        (c.get("primary_location") or {}).get("file") or "",
    ))

    result = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "findings": len(findings),
            "clusters": len(out_clusters),
            "reviewer_denominator": denominator,
            "gate_split": gated,
        },
        "clusters": out_clusters,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    tiers = {}
    for c in out_clusters:
        tiers[c["quorum"]] = tiers.get(c["quorum"], 0) + 1
    print(f"OK: {len(findings)} findings -> {len(out_clusters)} clusters "
          f"(denominator: {denominator} reviewers)")
    for q in sorted(tiers, reverse=True):
        print(f"  quorum {q}/{denominator}: {tiers[q]} cluster(s)")
    if gated:
        print(f"  confidence gate split: {', '.join(gated)}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
