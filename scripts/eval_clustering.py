#!/usr/bin/env python3
"""eval_clustering.py — measure the clustering judge against gold partitions.

The product's core claim is that reviewer quorum is real. A false merge
fabricates consensus, so merge precision is THE metric — this script makes it
measurable instead of vibes.

Modes:

1. Score a predicted partition against gold:
     eval_clustering.py gold.clusters.json predicted.clusters.json
   Reports pairwise same-cluster precision / recall / F1, lists every false
   merge (the dangerous errors) and missed merge (just noise), and the exact
   reproduction rate of gold multi-clusters.

2. Accumulate calibration data (add to mode 1):
     ... --calibration-log calibration.jsonl --fixture rubric-canon
   Appends one record per predicted multi-cluster: its match_confidence and
   whether all its pairs are correct. Over enough runs this answers whether
   the 0.7 confidence gate is set where the model's confidence actually means
   70% — LLM judges typically cluster confidences in 0.8–0.95, so the gate
   should be fit to observed reliability, not chosen a priori.

3. Report calibration from an accumulated log:
     eval_clustering.py --report-calibration calibration.jsonl

4. Order-stability across repeated runs (shuffle findings, re-cluster, compare):
     eval_clustering.py --stability pred1.json pred2.json [pred3.json ...]
   Reports Rand index and merged-pair Jaccard between runs, and lists the
   unstable pairs — merges that appear in some runs but not others. Those are
   exactly the borderline merges self-consistency should drop.

Exit code is 0 unless --min-precision is given and pairwise merge precision
falls below it (CI hook).
"""

import argparse
import itertools
import json
import sys
from collections import defaultdict


def load_partition(path):
    with open(path) as f:
        doc = json.load(f)
    clusters = doc["clusters"] if isinstance(doc, dict) else doc
    part = []
    for c in clusters:
        part.append({
            "cluster_id": c.get("cluster_id", "?"),
            "member_ids": list(c["member_ids"]),
            "match_confidence": c.get("match_confidence"),
        })
    return part


def same_cluster_pairs(partition):
    pairs = {}
    for c in partition:
        for a, b in itertools.combinations(sorted(c["member_ids"]), 2):
            pairs[(a, b)] = c
    return pairs


def all_ids(partition):
    return {m for c in partition for m in c["member_ids"]}


def rand_index(p1, p2):
    ids = sorted(all_ids(p1))
    s1, s2 = same_cluster_pairs(p1), same_cluster_pairs(p2)
    agree = total = 0
    for a, b in itertools.combinations(ids, 2):
        total += 1
        if (((a, b) in s1) == ((a, b) in s2)):
            agree += 1
    return agree / total if total else 1.0


def score(gold, pred, calibration_log=None, fixture=None):
    gids, pids = all_ids(gold), all_ids(pred)
    if gids != pids:
        missing = sorted(gids - pids)
        extra = sorted(pids - gids)
        print("warning: id mismatch between gold and predicted", file=sys.stderr)
        if missing:
            print(f"  missing from predicted: {missing}", file=sys.stderr)
        if extra:
            print(f"  extra in predicted:     {extra}", file=sys.stderr)

    gpairs = same_cluster_pairs(gold)
    ppairs = same_cluster_pairs(pred)

    tp = [p for p in ppairs if p in gpairs]
    false_merges = [p for p in ppairs if p not in gpairs]
    missed_merges = [p for p in gpairs if p not in ppairs]

    precision = len(tp) / len(ppairs) if ppairs else 1.0
    recall = len(tp) / len(gpairs) if gpairs else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # Exact reproduction of gold multi-clusters
    gold_multi = [frozenset(c["member_ids"]) for c in gold if len(c["member_ids"]) > 1]
    pred_sets = {frozenset(c["member_ids"]) for c in pred}
    exact = sum(1 for g in gold_multi if g in pred_sets)

    print(f"pairs: predicted {len(ppairs)}, gold {len(gpairs)}, correct {len(tp)}")
    print(f"merge precision: {precision:.3f}   (false merges fabricate consensus)")
    print(f"merge recall:    {recall:.3f}   (missed merges are just noise)")
    print(f"pairwise F1:     {f1:.3f}")
    print(f"gold multi-clusters reproduced exactly: {exact}/{len(gold_multi)}")

    if false_merges:
        print("\nFALSE MERGES (fix these first — candidates for new rubric examples):")
        for a, b in false_merges:
            c = ppairs[(a, b)]
            print(f"  {a} + {b}  in '{c['cluster_id']}' (confidence {c['match_confidence']})")
    if missed_merges:
        print("\nmissed merges:")
        for a, b in missed_merges:
            print(f"  {a} + {b}  (gold cluster '{gpairs[(a, b)]['cluster_id']}')")

    if calibration_log:
        records = []
        for c in pred:
            if len(c["member_ids"]) < 2:
                continue
            cpairs = list(itertools.combinations(sorted(c["member_ids"]), 2))
            correct = sum(1 for p in cpairs if p in gpairs)
            records.append({
                "fixture": fixture,
                "cluster_id": c["cluster_id"],
                "match_confidence": c["match_confidence"],
                "n_pairs": len(cpairs),
                "correct_pairs": correct,
                "pure": correct == len(cpairs),
            })
        with open(calibration_log, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        print(f"\nappended {len(records)} calibration record(s) to {calibration_log}")

    return precision


def report_calibration(path):
    buckets = defaultdict(lambda: [0, 0])  # bucket -> [pure, total]
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            conf = r.get("match_confidence")
            if not isinstance(conf, (int, float)):
                continue
            lo = min(int(conf * 10) / 10, 0.9)
            b = f"[{lo:.1f}, {lo + 0.1:.1f})" if lo < 0.9 else "[0.9, 1.0]"
            buckets[b][1] += 1
            buckets[b][0] += 1 if r.get("pure") else 0

    if not buckets:
        print("no calibration records yet")
        return
    print("stated confidence -> observed merge correctness")
    print(f"{'bucket':<14} {'n':>4} {'observed':>9}")
    for b in sorted(buckets):
        pure, total = buckets[b]
        print(f"{b:<14} {total:>4} {pure / total:>9.3f}")
    print(
        "\nIf a bucket's observed rate is far below its stated range, the judge is "
        "overconfident there — raise --confidence-gate in validate_partition.py to "
        "the lowest bucket whose observed rate you can live with."
    )


def stability(paths):
    parts = [(p, load_partition(p)) for p in paths]
    base_ids = all_ids(parts[0][1])
    for p, part in parts[1:]:
        if all_ids(part) != base_ids:
            print(f"error: {p} covers different finding ids than {parts[0][0]}",
                  file=sys.stderr)
            sys.exit(1)

    pair_sets = {p: set(same_cluster_pairs(part)) for p, part in parts}
    print(f"stability across {len(parts)} runs over {len(base_ids)} findings")
    for (p1, _), (p2, _) in itertools.combinations(parts, 2):
        s1, s2 = pair_sets[p1], pair_sets[p2]
        union = s1 | s2
        jac = len(s1 & s2) / len(union) if union else 1.0
        ri = rand_index(dict1(parts, p1), dict1(parts, p2))
        print(f"  {p1} vs {p2}: merged-pair Jaccard {jac:.3f}, Rand index {ri:.3f}")

    counts = defaultdict(int)
    for s in pair_sets.values():
        for pair in s:
            counts[pair] += 1
    unstable = [p for p, n in counts.items() if 0 < n < len(parts)]
    if unstable:
        print("\nUNSTABLE MERGES (present in some runs, absent in others — "
              "borderline calls that self-consistency should drop):")
        for a, b in sorted(unstable):
            print(f"  {a} + {b}  ({counts[(a, b)]}/{len(parts)} runs)")
    else:
        print("all merges stable across runs")


def dict1(parts, path):
    for p, part in parts:
        if p == path:
            return part
    raise KeyError(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gold", nargs="?", help="gold clusters.json")
    ap.add_argument("predicted", nargs="?", help="predicted clusters.json")
    ap.add_argument("--calibration-log", help="append calibration records (jsonl)")
    ap.add_argument("--fixture", help="fixture name for calibration records")
    ap.add_argument("--report-calibration", metavar="JSONL")
    ap.add_argument("--stability", nargs="+", metavar="PRED")
    ap.add_argument("--min-precision", type=float,
                    help="exit 1 if merge precision falls below this (CI hook)")
    args = ap.parse_args()

    if args.report_calibration:
        report_calibration(args.report_calibration)
        return
    if args.stability:
        stability(args.stability)
        return
    if not (args.gold and args.predicted):
        ap.error("need GOLD and PREDICTED (or --stability / --report-calibration)")

    precision = score(
        load_partition(args.gold), load_partition(args.predicted),
        calibration_log=args.calibration_log, fixture=args.fixture,
    )
    if args.min_precision is not None and precision < args.min_precision:
        print(f"\nFAIL: merge precision {precision:.3f} < {args.min_precision}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
