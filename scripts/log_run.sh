#!/usr/bin/env bash
# log_run.sh — snapshot a Quorum run into the dogfood log.
#
# usage: log_run.sh OWNER/REPO PR_NUMBER findings.json clusters.scored.json [LOG_DIR=.quorum/log]
#
# Writes LOG_DIR/<utc-date>-pr<N>.json with the run inputs plus an `outcomes`
# stub, one entry per cluster, for later labeling:
#
#   outcome:   "fixed" | "dismissed" | "false-positive" | "unknown"
#   bad_merge: true when a multi-finding cluster's members were NOT actually
#              the same issue (a fabricated-consensus event), else false/null
#
# These labels are the fuel for the eval flywheel:
#   - scripts/reviewer_reliability.py turns them into per-bot precision,
#     inter-bot agreement, and confidence calibration
#   - every bad_merge becomes a new worked example in the rubric and a new
#     fixture under evals/fixtures/
set -euo pipefail

usage() { echo "usage: log_run.sh OWNER/REPO PR_NUMBER findings.json clusters.scored.json [log_dir]" >&2; exit 2; }
[[ $# -ge 4 ]] || usage

REPO=$1
PR=$2
FINDINGS=$3
SCORED=$4
LOG_DIR=${5:-.quorum/log}

command -v jq >/dev/null || { echo "error: jq not found" >&2; exit 1; }
[[ -f "$FINDINGS" ]] || { echo "error: $FINDINGS not found" >&2; exit 1; }
[[ -f "$SCORED"   ]] || { echo "error: $SCORED not found" >&2; exit 1; }

mkdir -p "$LOG_DIR"
OUT="$LOG_DIR/$(date -u +%F)-pr${PR}.json"

jq -n \
  --arg repo "$REPO" \
  --arg pr "$PR" \
  --arg ts "$(date -u +%FT%TZ)" \
  --slurpfile findings "$FINDINGS" \
  --slurpfile scored "$SCORED" \
  '{
     repo: $repo,
     pr: ($pr | tonumber),
     logged_at: $ts,
     findings: $findings[0],
     scored: $scored[0],
     outcomes: ($scored[0].clusters
       | map({key: .cluster_id,
              value: {outcome: "unknown",
                      bad_merge: (if (.member_ids | length) > 1 then null else false end)}})
       | from_entries),
     labeling_guide: "outcome: fixed|dismissed|false-positive|unknown. bad_merge: true when a multi-finding cluster merged findings that were NOT the same issue."
   }' > "$OUT"

echo "logged run -> $OUT" >&2
echo "label outcomes when the PR settles: edit .outcomes in that file, then run scripts/reviewer_reliability.py" >&2
