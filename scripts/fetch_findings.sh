#!/usr/bin/env bash
# fetch_findings.sh — pull line-anchored review comments from a PR, keep the
# ones authored by review bots, and normalize them into Quorum finding records.
#
# usage: fetch_findings.sh OWNER/REPO PR_NUMBER [OUT=findings.json]
# env:   QUORUM_BOTS  case-insensitive regex matched against author login
#                     (default: 'cursor\[bot\]|copilot|devin|greptile|coderabbit')
#
# Output record (see normalize_findings.jq):
#   { id, reviewer, login, file, lines:[start,end], outdated,
#     body, hunk, url, comment_id, node_id,
#     pr, head, stated_severity }
set -euo pipefail

usage() { echo "usage: fetch_findings.sh OWNER/REPO PR_NUMBER [out.json]" >&2; exit 2; }
[[ $# -ge 2 ]] || usage

REPO=$1
PR=$2
OUT=${3:-findings.json}
BOTS_RE=${QUORUM_BOTS:-'cursor\[bot\]|copilot|devin|greptile|coderabbit'}

command -v gh >/dev/null || { echo "error: gh CLI not found" >&2; exit 1; }
command -v jq >/dev/null || { echo "error: jq not found" >&2; exit 1; }

RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

# --paginate emits one JSON document per page; --jq '.[]' flattens to a
# stream of comment objects, jq -s reassembles a single array.
gh api "repos/$REPO/pulls/$PR/comments" --paginate --jq '.[]' | jq -s '.' > "$RAW"

HEAD=$(gh api "repos/$REPO/pulls/$PR" --jq .head.sha)

TOTAL=$(jq 'length' "$RAW")
AUTHORS=$(jq -r '[.[].user.login] | unique | join(", ")' "$RAW")

jq --arg re "$BOTS_RE" --arg pr "$REPO#$PR" --arg head "$HEAD" \
   -f "$(dirname "$0")/normalize_findings.jq" "$RAW" > "$OUT"

N=$(jq 'length' "$OUT")
echo "PR #$PR: $TOTAL review comment(s) total; authors seen: ${AUTHORS:-none}" >&2
echo "Matched $N bot finding(s) -> $OUT" >&2
jq -r 'group_by(.reviewer)[] | "  \(.[0].reviewer): \(length)"' "$OUT" >&2 || true

if [[ "$N" -eq 0 ]]; then
  echo "" >&2
  echo "No findings matched filter '$BOTS_RE'." >&2
  echo "If review bots did comment, set QUORUM_BOTS to match the author logins listed above and re-run." >&2
fi
