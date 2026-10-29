"""quorum_merge.py — carry dispositions from a prior `quorum/triage@1` document
into a freshly clustered one.

Bots re-review after pushes, the PR head moves, and re-clustering can produce
different cluster ids. Finding identity is the GitHub review comment id
(`members[].comment_id`), which is stable, so a new cluster inherits from every
prior cluster it shares a comment id with. Pure functions: inputs are never
mutated.
"""
from __future__ import annotations

import copy
import re

from quorum_doc import latest_disposition, upgrade_legacy

# A recorded fixing commit: 7 to 40 lowercase hex characters, as `dispose --commit` takes it.
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")


def member_comment_ids(cluster: dict) -> set[int]:
    """The GitHub review comment ids of a cluster's members, as a set of ints."""
    return {m["comment_id"] for m in cluster.get("members") or [] if m.get("comment_id") is not None}


def _candidates(prior):
    """Everything in `prior` that holds a disposition and can pass it on: its
    clusters, then the orphans an earlier run left behind (which carry their
    original entry under `entry`). Each is {cluster_id, ids, disposition,
    history[, escalation][, entry]}."""
    found = []
    for c in prior["clusters"]:
        if latest_disposition(c):
            found.append({"cluster_id": c["cluster_id"], "ids": member_comment_ids(c),
                          "disposition": latest_disposition(c),
                          "history": c.get("disposition_history") or [],
                          **({"escalation": c["escalation"]} if "escalation" in c else {})})
    for o in prior.get("orphaned_dispositions") or []:
        if o.get("disposition"):
            found.append({"cluster_id": o.get("cluster_id"), "ids": set(o.get("member_comment_ids") or []),
                          "disposition": o["disposition"],
                          "history": o.get("disposition_history") or [],
                          **({"escalation": o["escalation"]} if "escalation" in o else {}),
                          "entry": o})
    return found


def _at(candidate):
    return candidate["disposition"].get("at") or ""


def _fixed_by_the_push(disposition, compare, new_head):
    """Whether the disposition's recorded `commit` (a 7 to 40 character SHA
    prefix) is the new head or one of the commits between the two heads."""
    commit = str(disposition.get("commit") or "")
    if not COMMIT_RE.fullmatch(commit):
        return False
    shas = [str(new_head or "")] + [str(sha) for sha in (compare or {}).get("commits") or []]
    return any(sha.startswith(commit) for sha in shas)


def _is_stale(cluster, head_moved, compare, new_head):
    """Whether a carried disposition must be re-checked. The push that carries
    the disposition's own fixing commit never makes it stale. Otherwise it is
    stale when the compare's files touch the cluster's `locations`, and
    `compare is None` means unknown (the compare call failed), so it counts as
    touched."""
    if not head_moved:
        return False
    if _fixed_by_the_push(cluster["disposition"], compare, new_head):
        return False
    if compare is None:
        return True
    files = {loc.get("file") for loc in cluster.get("locations") or []}
    return bool(files & set(compare.get("files") or []))


def inherit(new_doc: dict, prior_doc: dict | None, compare: dict | None) -> dict:
    """A copy of `new_doc` where each cluster that shares a member comment id
    with a prior cluster carries that prior cluster's disposition.

    The prior cluster with the newest `disposition.at` wins: the successor takes
    its `disposition`, `disposition_history` and `escalation`, and each other
    matching prior cluster's history and latest disposition are appended to the
    successor's history (a loser's escalation is dropped). Orphans left by an
    earlier run (`prior_doc["orphaned_dispositions"]`) match and compete the same
    way, by `member_comment_ids`. `compare` is what changed between the two
    heads, {"files": [filename], "commits": [sha]}, or None when unknown. A
    carried disposition gets `stale: True` when the head moved and `compare`
    names one of the successor's `locations[].file` (or is None), unless the
    disposition's recorded `commit` is a prefix of the new head or of one of
    `compare["commits"]`: the owner's own fix push does not reopen it.
    Dispositions with no successor stay in `orphaned_dispositions`: earlier
    orphans first, then this run's, deduped on their sorted comment ids (the
    first is kept). `timeline` is taken from the prior document when the new one
    has no `plan_at`. A legacy (pre-schema) prior document carries nothing.
    """
    out = copy.deepcopy(new_doc)
    if not prior_doc:
        return out
    prior = upgrade_legacy(prior_doc)

    candidates = _candidates(prior)
    head_moved = prior.get("head") != out.get("head")
    matched = set()

    for cluster in out["clusters"]:
        ids = member_comment_ids(cluster)
        # sorted() is stable, so equal `at` keeps candidate order: the first wins.
        matches = sorted((c for c in candidates if ids & c["ids"]), key=_at, reverse=True)
        if not matches:
            continue
        winner, losers = matches[0], matches[1:]
        matched.update(id(c) for c in matches)

        cluster["disposition"] = copy.deepcopy(winner["disposition"])
        history = copy.deepcopy(winner["history"])
        for loser in reversed(losers):  # oldest first
            history += copy.deepcopy(loser["history"])
            history.append(copy.deepcopy(loser["disposition"]))
        cluster["disposition_history"] = history
        if "escalation" in winner:
            cluster["escalation"] = copy.deepcopy(winner["escalation"])
        if _is_stale(cluster, head_moved, compare, out.get("head")):
            cluster["disposition"]["stale"] = True

    unmatched = [c for c in candidates if id(c) not in matched]
    earlier = [copy.deepcopy(c["entry"]) for c in unmatched if "entry" in c]
    fresh = [{"cluster_id": c["cluster_id"], "member_comment_ids": sorted(c["ids"]),
              "disposition": copy.deepcopy(c["disposition"])}
             for c in unmatched if "entry" not in c]
    orphans, seen = [], set()
    for o in (out.get("orphaned_dispositions") or []) + earlier + fresh:
        key = tuple(sorted(o.get("member_comment_ids") or []))
        if key not in seen:
            seen.add(key)
            orphans.append(o)
    out["orphaned_dispositions"] = orphans

    if not (out.get("timeline") or {}).get("plan_at") and prior.get("timeline"):
        out["timeline"] = copy.deepcopy(prior["timeline"])
    return out
