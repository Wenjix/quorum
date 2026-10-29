"""quorum_doc.py — pure helpers over the `quorum/triage@1` document.

The document is the machine-readable JSON embedded in the synthesis comment
(see validate_partition.py for the envelope it builds). Nothing here touches
the network or the filesystem; `now_iso()` only reads the clock.
"""

import copy
import datetime
import json
import re

# One source of truth for the schema id and the lane / P-tag rules.
from validate_partition import LANES, P_TAG, SCHEMA, lane_for

MARKER = "<!-- quorum:synthesis -->"

# A fenced ```json block. The closing fence must start a line, so a "```"
# inside a JSON string (where newlines are escaped) cannot end the block.
JSON_FENCE_RE = re.compile(r"^[ \t]*```json[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
                           re.DOTALL | re.MULTILINE | re.IGNORECASE)


def extract_doc(body):
    """The last fenced json block in `body` that parses as an object with a
    `clusters` list, or None. The synthesis comment may carry other json
    blocks (the protocol examples); the document is the last one."""
    found = None
    for block in JSON_FENCE_RE.findall((body or "").replace("\r\n", "\n")):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("clusters"), list):
            found = parsed
    return found


def is_triage(doc):
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA


def latest_disposition(cluster):
    return cluster.get("disposition")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def locations_from_members(members):
    """Each member's anchored location as {file, lines, from: member id},
    deduped on (file, lines) with the first member to name it as `from`."""
    locations, seen = [], set()
    for member in members:
        if not member.get("file"):
            continue
        lines = list(member.get("lines") or [None, None])
        key = (member["file"], tuple(lines))
        if key not in seen:
            seen.add(key)
            locations.append({"file": member["file"], "lines": lines, "from": member.get("id")})
    return locations


def upgrade_legacy(doc):
    """A `quorum/triage@1` copy of an old clusters.scored.json, shaped like
    validate_partition.py's own output. A document that already carries the
    schema comes back unchanged (as a copy). The input is never mutated."""
    out = copy.deepcopy(doc)
    if is_triage(out):
        return out

    reviewer_counts = {}
    for c in out["clusters"]:
        members = c.get("members") or []
        quorum = c.get("quorum") or len(c.get("reviewers") or [])
        c.setdefault("lane", lane_for(quorum))
        c.setdefault("p_tag", P_TAG.get(c.get("severity")))
        c.setdefault("validation", {"mode": LANES[c["lane"]]})
        c.setdefault("locations", locations_from_members(members))
        c.setdefault("mechanisms_claimed", [])
        c.setdefault("disposition_history", [])
        for m in members:
            if m.get("reviewer"):
                reviewer_counts[m["reviewer"]] = reviewer_counts.get(m["reviewer"], 0) + 1

    out["schema"] = SCHEMA
    for key, default in (("pr", None), ("head", None), ("generated_at", None), ("priors", {}),
                         ("deferred", []), ("timeline", {}), ("orphaned_dispositions", [])):
        out.setdefault(key, default)
    out.setdefault("reviewers", dict(sorted(reviewer_counts.items())))
    out.setdefault("lanes", {
        lane: [c["cluster_id"] for c in out["clusters"] if c["lane"] == lane] for lane in LANES
    })
    return out
