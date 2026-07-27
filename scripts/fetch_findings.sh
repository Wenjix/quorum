#!/usr/bin/env bash
# fetch_findings.sh — pull line-anchored review comments from a PR, keep the
# ones authored by review bots, and normalize them into Quorum finding records.
#
# usage: fetch_findings.sh OWNER/REPO PR_NUMBER [OUT=findings.json]
# env:   QUORUM_BOTS          case-insensitive regex matched against author login
#                             (default: 'cursor\[bot\]|copilot|devin')
#        QUORUM_ALLOW_HUMANS  set to 1 to also match accounts whose type is not
#                             "Bot". Escape hatch for reviewers that post from
#                             regular user accounts. Off by default so a human
#                             whose login happens to match the regex (e.g.
#                             "devinsmith") can't be counted as a reviewer and
#                             inflate the quorum denominator.
#
# Output record:
#   { id, reviewer, login, file, lines:[start,end], outdated,
#     body, hunk, url, comment_id, node_id,
#     reply_count, human_reply_count,
#     reactions: {total, plus_one, minus_one} }
#
# Design notes:
# - IDs are per-reviewer ordinals assigned in comment-creation order (ascending
#   comment id). New comments on a re-run get new ids appended at the end
#   instead of reshuffling existing ids, so dogfood logs and re-runs stay
#   comparable. (Deleting a comment still shifts later ids; comment_id is the
#   fully stable key.)
# - Thread replies (in_reply_to_id set) are not separate findings — they are
#   folded into the parent finding as reply_count / human_reply_count. A human
#   reply or reaction on a bot comment is validity signal for the eval
#   flywheel; it never affects quorum.
set -euo pipefail

usage() { echo "usage: fetch_findings.sh OWNER/REPO PR_NUMBER [out.json]" >&2; exit 2; }
[[ $# -ge 2 ]] || usage

REPO=$1
PR=$2
OUT=${3:-findings.json}
BOTS_RE=${QUORUM_BOTS:-'cursor\[bot\]|copilot|devin'}
ALLOW_HUMANS=${QUORUM_ALLOW_HUMANS:-0}

command -v gh >/dev/null || { echo "error: gh CLI not found" >&2; exit 1; }
command -v jq >/dev/null || { echo "error: jq not found" >&2; exit 1; }

RAW=$(mktemp)
trap 'rm -f "$RAW"' EXIT

# --paginate emits one JSON document per page; --jq '.[]' flattens to a
# stream of comment objects, jq -s reassembles a single array.
gh api "repos/$REPO/pulls/$PR/comments" --paginate --jq '.[]' | jq -s '.' > "$RAW"

TOTAL=$(jq 'length' "$RAW")
AUTHORS=$(jq -r '[.[].user.login] | unique | join(", ")' "$RAW")

jq --arg re "$BOTS_RE" --arg allow "$ALLOW_HUMANS" '
  def short(l): (l | ascii_downcase) as $x
    | if   ($x | test("cursor"))  then "bugbot"
      elif ($x | test("copilot")) then "copilot"
      elif ($x | test("devin"))   then "devin"
      else ($x | gsub("\\[bot\\]$"; "") | gsub("[^a-z0-9]+"; "-"))
      end;

  . as $all
  | [ $all[] | select(.in_reply_to_id != null) ] as $replies
  | [ $all[]
      | select(.in_reply_to_id == null)
      | select(.user.login | test($re; "i"))
      | select(((.user.type // "") == "Bot") or ($allow == "1"))
    ]
  | sort_by(.id)                       # creation order -> stable ids across re-runs
  | group_by(.user.login)
  | map(
      to_entries
      | map(
          .value as $c
          | [ $replies[] | select(.in_reply_to_id == $c.id) ] as $r
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
              node_id:  $c.node_id,
              reply_count: ($r | length),
              human_reply_count:
                ([ $r[] | select((.user.type // "") != "Bot") ] | length),
              reactions: {
                total:     ($c.reactions.total_count // 0),
                plus_one:  ($c.reactions["+1"] // 0),
                minus_one: ($c.reactions["-1"] // 0)
              }
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

# Surface regex-matching accounts that were excluded because they are not
# type "Bot" — the operator can re-run with QUORUM_ALLOW_HUMANS=1 if one of
# them is a legitimate reviewer posting from a user account.
if [[ "$ALLOW_HUMANS" != "1" ]]; then
  SKIPPED=$(jq -r --arg re "$BOTS_RE" '
    [ .[] | select(.in_reply_to_id == null)
          | select(.user.login | test($re; "i"))
          | select((.user.type // "") != "Bot")
          | .user.login ] | unique | join(", ")' "$RAW")
  if [[ -n "$SKIPPED" ]]; then
    echo "note: skipped non-Bot account(s) matching the filter: $SKIPPED" >&2
    echo "      set QUORUM_ALLOW_HUMANS=1 if one of these is a real review bot." >&2
  fi
fi

if [[ "$N" -eq 0 ]]; then
  echo "" >&2
  echo "No findings matched filter '$BOTS_RE'." >&2
  echo "If review bots did comment, set QUORUM_BOTS to match the author logins listed above and re-run." >&2
fi
