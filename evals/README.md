# Clustering evals

Quorum's value rests on one claim: reviewer quorum is real. A false merge fabricates consensus, so **merge precision is the metric** — this directory makes it measurable.

## Layout

```text
evals/
├── fixtures/
│   ├── rubric-canon/       # encodes the rubric's worked examples (A, B, C) plus
│   │   ├── findings.json   # a within-reviewer dup and a cross-file merge
│   │   └── gold.clusters.json
│   └── near-miss-traps/    # adversarial: same-line-different-issue, similar-wording-
│       ├── findings.json   # independent-fixes, functional-vs-style co-location
│       └── gold.clusters.json
└── README.md
```

Gold files use the normal `clusters.json` schema (including `single_fix` on multi-finding clusters). Some gold clusters carry a `trap` field documenting the failure mode they guard against — the validator ignores unknown fields.

## Scoring a run

Have the judge cluster a fixture's `findings.json` per `references/clustering-rubric.md`, then:

```bash
python3 scripts/eval_clustering.py \
  evals/fixtures/rubric-canon/gold.clusters.json predicted.clusters.json
```

Reports pairwise merge precision / recall / F1, lists every **false merge** (the dangerous error — each one is a candidate worked example for the rubric) and missed merge (just noise), and the exact reproduction rate of gold multi-clusters.

CI-style gate: add `--min-precision 1.0` to make the run fail on any false merge.

## Calibration: is the 0.7 confidence gate real?

The `--confidence-gate` in `validate_partition.py` is only meaningful if the judge's stated confidence tracks observed correctness. LLM judges typically cluster confidences in 0.8–0.95, so fit the gate to data:

```bash
# accumulate records across fixture runs and dogfood cases
python3 scripts/eval_clustering.py GOLD PRED \
  --calibration-log evals/calibration.jsonl --fixture rubric-canon

# then inspect stated confidence vs observed merge correctness
python3 scripts/eval_clustering.py --report-calibration evals/calibration.jsonl
```

Set the gate to the lowest confidence bucket whose observed correctness you can live with. (`scripts/reviewer_reliability.py` produces the same table from labeled dogfood logs — real-PR data beats fixtures once you have it.)

## Order stability

Judges can be order-sensitive. Shuffle `findings.json`, re-cluster, and compare partitions:

```bash
python3 scripts/eval_clustering.py --stability pred-run1.json pred-run2.json pred-run3.json
```

Reports Rand index and merged-pair Jaccard between runs, and lists **unstable merges** — pairs merged in some runs but not others. Those are the borderline calls; a self-consistency pass (merge only if reproduced) drops exactly them.

## Growing the fixture set

Every bad merge caught in the wild is the next fixture: `scripts/log_run.sh` snapshots each real run, and labeling `bad_merge: true` in the log identifies the case to distill. Copy the findings involved into a new fixture directory with a corrected gold partition, and add the pattern as a worked example in the rubric. The skill improves from exactly the failures that matter.
