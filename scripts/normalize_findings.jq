# normalize_findings.jq — turn raw GitHub PR review comments into Quorum
# finding records. Invoked by fetch_findings.sh as:
#
#   jq --arg re "$BOTS_RE" --arg pr "$REPO#$PR" --arg head "$HEAD" \
#      -f normalize_findings.jq raw.json
#
# Input:  JSON array of review-comment objects (gh api .../pulls/N/comments).
#         A comment is kept only if its author is a bot account (user.type is
#         "Bot") AND the login matches $re. The regex is an unanchored
#         substring match, so the Bot-type requirement is what stops a human
#         whose login merely contains a bot name (e.g. "devin-fan") from being
#         ingested as a reviewer finding.
# Args:   $re    case-insensitive regex matched against the author login
#         $pr    "OWNER/REPO#N"
#         $head  PR head commit sha
#
# Output record:
#   { id, reviewer, login, file, lines:[start,end], outdated,
#     body, hunk, url, comment_id, node_id,
#     pr, head, stated_severity }
#
# stated_severity is what the reviewer itself said about the finding, mapped to
# "critical" | "major" | "minor" | "nit", or null when it said nothing we
# recognise.

def short(l): (l | ascii_downcase) as $x
  | if   ($x | test("cursor"))     then "bugbot"
    elif ($x | test("copilot"))    then "copilot"
    elif ($x | test("devin"))      then "devin"
    elif ($x | test("greptile"))   then "greptile"
    elif ($x | test("coderabbit")) then "coderabbit"
    else ($x | gsub("\\[bot\\]$"; "") | gsub("[^a-z0-9]+"; "-"))
    end;

# Severity the reviewer stated in its own comment body.
#   bugbot    **High Severity** / **Medium Severity** / **Low Severity**
#   devin     body opens with <!-- devin-review-comment {"id": "BUG_...  -> major
#                                                         "id": "ANALYSIS_... -> minor
#   greptile  P0 / P1 / P2 / P3 in the first 200 chars of the body
#             -> critical / major / minor / nit.
#             ASSUMPTION: the Greptile P0-P3 convention and the \bP([0-3])\b
#             pattern are unverified. Check them against a real Greptile line
#             comment before relying on this branch.
# Any other reviewer, or a body with no recognised marker, yields null.
def stated(reviewer; body):
  (body // "") as $b
  | if reviewer == "bugbot" then
      if   ($b | test("\\*\\*High Severity\\*\\*"))   then "critical"
      elif ($b | test("\\*\\*Medium Severity\\*\\*")) then "major"
      elif ($b | test("\\*\\*Low Severity\\*\\*"))    then "minor"
      else null
      end
    elif reviewer == "devin" then
      if   ($b | test("^<!-- devin-review-comment \\{\"id\": \"BUG_"))      then "major"
      elif ($b | test("^<!-- devin-review-comment \\{\"id\": \"ANALYSIS_")) then "minor"
      else null
      end
    elif reviewer == "greptile" then
      (($b[:200] | capture("\\bP(?<n>[0-3])\\b") | .n) // null) as $n
      | if   $n == "0" then "critical"
        elif $n == "1" then "major"
        elif $n == "2" then "minor"
        elif $n == "3" then "nit"
        else null
        end
    else null
    end;

[ .[] | select(.user.type == "Bot" and (.user.login | test($re; "i"))) ]
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
            node_id:  $c.node_id,
            pr:       $pr,
            head:     $head,
            stated_severity: stated(short($c.user.login); $c.body)
          }
      )
  )
| add // []
| sort_by(.file, (.lines[0] // 0))
