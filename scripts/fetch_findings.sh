#!/usr/bin/env bash
# fetch_findings.sh — pull line-anchored review comments from a PR, keep the
# ones authored by review bots, and normalize them into Quorum finding records.
#
# usage: fetch_findings.sh OWNER/REPO PR_NUMBER [OUT=findings.json]
# env:   QUORUM_BOTS  case-insensitive regex matched against author login
#                     (default: 'cursor\[bot\]|copilot|devin')
#
# Output record:
#   { id, reviewer, login, file, lines:[start,end], outdated,
#     body, hunk, url, comment_id, node_id }
set -euo pipefail

usage() { echo "usage: fetch_findings.sh OWNER/REPO PR_NUMBER [out.json]" >&2; exit 2; }
[[ $# -ge 2 ]] || usage

REPO=$1
PR=$2
OUT=${3:-findings.json}
BOTS_RE=${QUORUM_BOTS:-'cursor\[bot\]|copilot|devin'}

command -v gh >/dev/null || { echo "error: gh CLI not found" >&2; exit 1; }
command -v jq >/dev/null || { echo "error: jq not found" >&2; exit 1; }

RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

# --paginate emits one JSON document per page; --jq '.[]' flattens to a
# stream of comment objects, jq -s reassembles a single array.
gh api "repos/$REPO/pulls/$PR/comments" --paginate --jq '.[]' | jq -s '.' > "$RAW"

TOTAL=$(jq 'length' "$RAW")
AUTHORS=$(jq -r '[.[].user.login] | unique | join(", ")' "$RAW")

jq --arg re "$BOTS_RE" '
  def short(l): (l | ascii_downcase) as $x
    | if   ($x | test("cursor"))  then "bugbot"
      elif ($x | test("copilot")) then "copilot"
      elif ($x | test("devin"))   then "devin"
      else ($x | gsub("\\[bot\\]$"; "") | gsub("[^a-z0-9]+"; "-"))
      end;

  [ .[] | select(.user.login | test($re; "i")) ]
  | sort_by(.path, (.line // .original_line // 0))
  | group_by(.user.login)
  | map(
      to_entries
      | map(
          .value as $c
          | {
              id:       (short($c.user.login) + "-" + ((.key + 1) | tostring)),
              reviewer: short($c.user.login),
              login:    $c.user.login,
              file:     $c.path,
              lines: [
                ($c.start_line // $c.original_start_line // $c.line // $c.original_line),
                ($c.line // $c.original_line // $c.start_line // $c.original_start_line)
              ],
              outdated: ($c.line == null),
              body:     $c.body,
              hunk:     ($c.diff_hunk // ""),
              url:      $c.html_url,
              comment_id: $c.id,
              node_id:  $c.node_id
            }
        )
    )
  | add // []
  | sort_by(.file, (.lines[0] // 0))
' "$RAW" > "$OUT"

N=$(jq 'length' "$OUT")
echo "PR #$PR: $TOTAL review comment(s) total; authors seen: ${AUTHORS:-none}" >&2
echo "Matched $N bot finding(s) -> $OUT" >&2
jq -r 'group_by(.reviewer)[] | "  \(.[0].reviewer): \(length)"' "$OUT" >&2 || true

if [[ "$N" -eq 0 ]]; then
  echo "" >&2
  echo "No findings matched filter '$BOTS_RE'." >&2
  echo "If review bots did comment, set QUORUM_BOTS to match the author logins listed above and re-run." >&2
fi
