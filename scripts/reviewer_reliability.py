#!/usr/bin/env python3
"""reviewer_reliability.py — turn labeled dogfood logs into reviewer statistics.

Quorum v0 treats reviewers as exchangeable and independent. They are neither:
bots share training data, so their errors correlate and raw agreement counts
overstate consensus. This script computes, from logs written by
scripts/log_run.sh and labeled by the operator:

  1. Per-reviewer volume and corroboration rate (share of a bot's findings
     that land in quorum >= 2 clusters).
  2. Per-reviewer and per-category precision from outcome labels
     (fixed / dismissed / false-positive). This is the empirical input a
     weighted-quorum scheme needs — the natural next step is a Dawid-Skene
     style annotator model over these counts, but honest empirical rates
     come first.
  3. Inter-reviewer co-occurrence (cluster-level Jaccard). High co-occurrence
     with correlated false positives means a 2-reviewer quorum is worth less
     than it looks; this quantifies how much.
  4. Confidence calibration for merges, from bad_merge labels: stated
     match_confidence bucket vs observed correct-merge rate — the data that
     should set validate_partition.py's --confidence-gate.
  5. Human-signal check: findings with human replies or +1 reactions vs
     outcome, where those fields exist (fetch_findings.sh captures them).

usage: reviewer_reliability.py [--log-dir .quorum/log] [--json out.json]
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict


def load_logs(log_dir):
    logs = []
    for path in sorted(glob.glob(os.path.join(log_dir, "*.json"))):
        try:
            with open(path) as f:
                log = json.load(f)
            if "scored" in log and "findings" in log:
                logs.append((path, log))
        except (OSError, json.JSONDecodeError) as e:
            print(f"warning: skipping {path}: {e}", file=sys.stderr)
    return logs


def bucket(conf):
    if not isinstance(conf, (int, float)):
        return None
    lo = min(int(conf * 10) / 10, 0.9)
    return f"[{lo:.1f}, {lo + 0.1:.1f})" if lo < 0.9 else "[0.9, 1.0]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=".quorum/log")
    ap.add_argument("--json", help="also write machine-readable stats here")
    args = ap.parse_args()

    logs = load_logs(args.log_dir)
    if not logs:
        print(f"no run logs found in {args.log_dir} — run scripts/log_run.sh after a synthesis")
        return

    volume = defaultdict(int)                 # reviewer -> findings
    corroborated = defaultdict(int)           # reviewer -> findings in quorum>=2 clusters
    outcome_counts = defaultdict(lambda: defaultdict(int))   # reviewer -> outcome -> n findings
    cat_outcomes = defaultdict(lambda: defaultdict(int))     # (reviewer, category) -> outcome
    cluster_sets = defaultdict(set)           # reviewer -> set of (log, cluster_id)
    calib = defaultdict(lambda: [0, 0])       # bucket -> [good, total] labeled merges
    human_signal = defaultdict(lambda: defaultdict(int))     # signal -> outcome -> n

    labeled_any = False
    for path, log in logs:
        outcomes = log.get("outcomes") or {}
        fmap = {f["id"]: f for f in log["findings"]}
        for c in log["scored"].get("clusters", []):
            cid = c["cluster_id"]
            label = (outcomes.get(cid) or {})
            outcome = label.get("outcome", "unknown")
            bad_merge = label.get("bad_merge")
            if outcome != "unknown":
                labeled_any = True

            for r in c.get("reviewers", []):
                cluster_sets[r].add((path, cid))
            for mid in c.get("member_ids", []):
                f = fmap.get(mid) or {}
                r = f.get("reviewer", "?")
                volume[r] += 1
                if c.get("quorum", 1) >= 2:
                    corroborated[r] += 1
                if outcome != "unknown":
                    outcome_counts[r][outcome] += 1
                    cat_outcomes[(r, c.get("category", "other"))][outcome] += 1
                if outcome != "unknown":
                    if (f.get("human_reply_count") or 0) > 0:
                        human_signal["human reply"][outcome] += 1
                    if ((f.get("reactions") or {}).get("plus_one") or 0) > 0:
                        human_signal["+1 reaction"][outcome] += 1

            if len(c.get("member_ids", [])) > 1 and isinstance(bad_merge, bool):
                b = bucket(c.get("match_confidence"))
                if b:
                    calib[b][1] += 1
                    calib[b][0] += 0 if bad_merge else 1

    print(f"{len(logs)} run log(s) from {args.log_dir}\n")

    print("reviewer volume and corroboration:")
    print(f"  {'reviewer':<12} {'findings':>8} {'in quorum>=2':>13}")
    for r in sorted(volume):
        share = corroborated[r] / volume[r] if volume[r] else 0.0
        print(f"  {r:<12} {volume[r]:>8} {corroborated[r]:>6} ({share:.0%})")

    def precision(counts):
        fixed = counts.get("fixed", 0)
        fp = counts.get("false-positive", 0)
        return (fixed / (fixed + fp)) if (fixed + fp) else None

    if labeled_any:
        print("\nlabeled precision (fixed / (fixed + false-positive)):")
        for r in sorted(outcome_counts):
            p = precision(outcome_counts[r])
            n = sum(outcome_counts[r].values())
            ps = f"{p:.2f}" if p is not None else "n/a"
            print(f"  {r:<12} precision {ps}  (labeled findings: {n}, "
                  f"{dict(outcome_counts[r])})")

        by_cat = defaultdict(list)
        for (r, cat), counts in cat_outcomes.items():
            p = precision(counts)
            if p is not None:
                by_cat[cat].append((r, p, sum(counts.values())))
        if by_cat:
            print("\nper-category precision:")
            for cat in sorted(by_cat):
                cells = ", ".join(f"{r}: {p:.2f} (n={n})" for r, p, n in sorted(by_cat[cat]))
                print(f"  {cat:<15} {cells}")
    else:
        print("\nno outcome labels yet — edit `outcomes` in the log files "
              "(fixed | dismissed | false-positive, bad_merge on multi-clusters).")

    reviewers = sorted(cluster_sets)
    if len(reviewers) > 1:
        print("\ninter-reviewer co-occurrence (cluster-level Jaccard; high values with "
              "shared false positives = correlated errors, quorum worth less):")
        for i in range(len(reviewers)):
            for j in range(i + 1, len(reviewers)):
                a, b = reviewers[i], reviewers[j]
                sa, sb = cluster_sets[a], cluster_sets[b]
                union = sa | sb
                jac = len(sa & sb) / len(union) if union else 0.0
                print(f"  {a} ~ {b}: {jac:.2f}  (shared clusters: {len(sa & sb)})")

    if calib:
        print("\nmerge-confidence calibration (stated confidence vs labeled good merges):")
        print(f"  {'bucket':<14} {'n':>4} {'observed':>9}")
        for b in sorted(calib):
            good, total = calib[b]
            print(f"  {b:<14} {total:>4} {good / total:>9.2f}")
        print("  use this to set validate_partition.py --confidence-gate from data.")

    if human_signal:
        print("\nhuman signal vs outcome (validity proxy):")
        for sig in sorted(human_signal):
            print(f"  {sig}: {dict(human_signal[sig])}")

    if args.json:
        stats = {
            "runs": len(logs),
            "volume": dict(volume),
            "corroborated": dict(corroborated),
            "outcomes": {r: dict(c) for r, c in outcome_counts.items()},
            "calibration": {b: {"good": g, "total": t} for b, (g, t) in calib.items()},
        }
        with open(args.json, "w") as f:
            json.dump(stats, f, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
