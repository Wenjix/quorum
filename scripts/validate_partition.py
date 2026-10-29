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

4. Emits the `quorum/triage@1` document: a superset of the old
   clusters.scored.json that adds the PR identity, per-reviewer counts, the
   agreed/solo lanes (quorum >= 2 is "agreed"), and per cluster a lane,
   P-tag, validation mode and the union of every member's locations.

usage: validate_partition.py findings.json clusters.json [-o clusters.scored.json]
       [--confidence-gate 0.7] [--no-gate] [--priors priors.json]
       validate_partition.py findings.json --all-singletons [-o ...]
         (skips the clustering judge: one singleton cluster per finding)
"""

import argparse
import datetime
import json
import re
import sys

SCHEMA = "quorum/triage@1"
SEVERITIES = {"critical": 3, "major": 2, "minor": 1, "nit": 0}
CATEGORIES = {
    "logic", "concurrency", "security", "performance", "error-handling",
    "data-integrity", "api-contract", "style", "docs", "test-gap", "other",
}
MATCH_TYPES = {
    "exact", "same-root-cause", "general-specific",
    "within-reviewer-dup", "singleton",
}
# Display shorthand for severity; the severity words stay on the cluster.
P_TAG = {"critical": "P1", "major": "P1", "minor": "P2", "nit": "P3"}
# lane -> validation mode: agreement directs how hard a cluster is validated.
LANES = {"agreed": "escalate", "solo": "prove_or_kill"}
MECHANISM_AGREEMENT = {"compatible", "split", "unknown"}

# Bugbot embeds extra anchors as `path#L10-L12` / `path#L10` lines in this block.
LOCATIONS_BLOCK_RE = re.compile(r"LOCATIONS START(.*?)LOCATIONS END", re.DOTALL)
LOCATION_LINE_RE = re.compile(r"^(\S+?)#L(\d+)(?:-L(\d+))?$")


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


def singleton_from(finding, parent_id=None, parent_severity=None):
    lines = finding.get("lines") or [None, None]
    stated = finding.get("stated_severity")
    stated_severity = stated if stated in SEVERITIES else None
    c = {
        "cluster_id": f"{finding['id']}-solo",
        "member_ids": [finding["id"]],
        "canonical_title": first_line(finding.get("body")),
        "canonical_description": first_line(finding.get("body"), 240),
        "category": "other",
        # Preserve the parent cluster's severity when the confidence gate splits
        # it, so a low-confidence cluster does not silently downgrade a critical
        # finding to "minor". Genuine standalone singletons take the reviewer's
        # own stated severity and fall back to "minor" when it states none.
        "severity": parent_severity or stated_severity or "minor",
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


def singletons_doc(findings):
    """A clusters document with one singleton per finding — the fallback when
    the clustering judge is skipped."""
    return {"clusters": [singleton_from(f) for f in findings]}


def lane_for(quorum):
    return "agreed" if quorum >= 2 else "solo"


def parse_extra_locations(body):
    """Extra anchors a reviewer embedded in its comment body (Bugbot's
    LOCATIONS START ... LOCATIONS END block), as [{file, lines: [a, b]}]."""
    found = []
    for block in LOCATIONS_BLOCK_RE.findall(body or ""):
        for line in block.splitlines():
            m = LOCATION_LINE_RE.match(line.strip())
            if m:
                start = int(m.group(2))
                found.append({"file": m.group(1), "lines": [start, int(m.group(3) or start)]})
    return found


def union_locations(cluster, fmap):
    """Every member's anchored location plus the extras parsed from its body,
    deduped on (file, lines); the first member to name a location is its `from`."""
    locations, seen = [], set()
    for mid in cluster["member_ids"]:
        finding = fmap[mid]
        candidates = []
        if finding.get("file"):
            candidates.append({"file": finding["file"], "lines": list(finding.get("lines") or [None, None])})
        candidates += parse_extra_locations(finding.get("body"))
        for loc in candidates:
            key = (loc["file"], tuple(loc["lines"]))
            if key not in seen:
                seen.add(key)
                locations.append({**loc, "from": mid})
    return locations


def mechanisms_ok(claimed, member_ids):
    """True when `claimed` has exactly one well-formed entry per member id."""
    if not isinstance(claimed, list) or len(claimed) != len(member_ids):
        return False
    for entry in claimed:
        if not isinstance(entry, dict):
            return False
        for field in ("reviewer", "member_id", "mechanism"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
    return sorted(e["member_id"] for e in claimed) == sorted(member_ids)


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
        if len(mids) > 1:
            if not mechanisms_ok(c.get("mechanisms_claimed"), mids):
                errors.append(f"{cid}: mechanisms_claimed must have one entry per member")
            agreement = c.get("mechanism_agreement")
            if agreement not in MECHANISM_AGREEMENT:
                errors.append(
                    f"{cid}: mechanism_agreement '{agreement}' not in {sorted(MECHANISM_AGREEMENT)}")

    omitted = [fid for fid in fmap if fid not in seen]
    for fid in omitted:
        errors.append(f"finding '{fid}' omitted from all clusters")

    return errors, clusters


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("findings")
    ap.add_argument("clusters", nargs="?",
                    help="clustering judge output; ignored (may be '-') with --all-singletons")
    ap.add_argument("-o", "--out", default="clusters.scored.json")
    ap.add_argument("--confidence-gate", type=float, default=0.7)
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--all-singletons", action="store_true",
                    help="skip the clustering judge: one singleton cluster per finding")
    ap.add_argument("--priors", help="JSON file of per-reviewer priors to embed")
    args = ap.parse_args()
    if not args.all_singletons and not args.clusters:
        ap.error("the clusters argument is required unless --all-singletons is given")

    findings = load(args.findings, "findings")
    doc = singletons_doc(findings) if args.all_singletons else load(args.clusters, "clusters")
    priors = {}
    if args.priors:
        priors = load(args.priors, "priors")
        # Accept the priors tool's output ({"reviewers": {...}, ...}) or a bare map.
        if isinstance(priors, dict) and "reviewers" in priors:
            priors = priors["reviewers"]
        if not isinstance(priors, dict):
            print(f"error: priors in {args.priors} must be a JSON object", file=sys.stderr)
            sys.exit(1)
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
            out_clusters.extend(
                singleton_from(fmap[m], c["cluster_id"], c.get("severity"))
                for m in c["member_ids"]
            )
        else:
            out_clusters.append(dict(c))

    # Deterministic quorum + enrichment
    for c in out_clusters:
        reviewers = sorted({fmap[m]["reviewer"] for m in c["member_ids"]})
        c["quorum"] = len(reviewers)
        c["reviewers"] = reviewers
        c["lane"] = lane_for(c["quorum"])
        c["p_tag"] = P_TAG[c["severity"]]
        c["validation"] = {"mode": LANES[c["lane"]]}
        cheapest_check = c.pop("cheapest_check", None)
        if cheapest_check:
            c["validation"]["cheapest_check"] = cheapest_check
        c["locations"] = union_locations(c, fmap)
        c.setdefault("mechanisms_claimed", [])
        c["disposition_history"] = []
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

    reviewer_counts = {}
    for f in findings:
        reviewer_counts[f["reviewer"]] = reviewer_counts.get(f["reviewer"], 0) + 1

    result = {
        "schema": SCHEMA,
        "pr": findings[0].get("pr") if findings else None,
        "head": findings[0].get("head") if findings else None,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "findings": len(findings),
            "clusters": len(out_clusters),
            "reviewer_denominator": denominator,
            "gate_split": gated,
        },
        "reviewers": dict(sorted(reviewer_counts.items())),
        "priors": priors,
        "lanes": {
            lane: [c["cluster_id"] for c in out_clusters if c["lane"] == lane]
            for lane in LANES
        },
        "clusters": out_clusters,
        "deferred": [],
        "timeline": {},
        "orphaned_dispositions": [],
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
