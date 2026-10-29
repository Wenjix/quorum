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

from quorum_doc import latest_disposition, upgrade_legacy


def member_comment_ids(cluster: dict) -> set[int]:
    """The GitHub review comment ids of a cluster's members, as a set of ints."""
    return {m["comment_id"] for m in cluster.get("members") or [] if m.get("comment_id") is not None}


def _at(cluster: dict) -> str:
    return latest_disposition(cluster).get("at") or ""


def _is_stale(cluster, head_moved, changed_files):
    """Whether a carried disposition must be re-checked. `changed_files is None`
    means unknown (the compare call failed), so it counts as touched."""
    if not head_moved:
        return False
    if changed_files is None:
        return True
    files = {loc.get("file") for loc in cluster.get("locations") or []}
    return bool(files & set(changed_files))


def inherit(new_doc: dict, prior_doc: dict | None, changed_files: list[str] | None) -> dict:
    """A copy of `new_doc` where each cluster that shares a member comment id
    with a prior cluster carries that prior cluster's disposition.

    The prior cluster with the newest `disposition.at` wins: the successor takes
    its `disposition`, `disposition_history` and `escalation`, and each other
    matching prior cluster's history and latest disposition are appended to the
    successor's history. A carried disposition gets `stale: True` when the head
    moved and `changed_files` (the files changed between the two heads, or None
    when unknown) names one of the successor's `locations[].file`. Prior
    clusters with a disposition and no successor go to `orphaned_dispositions`.
    `timeline` is taken from the prior document when the new one has no
    `plan_at`. A legacy (pre-schema) prior document carries nothing.
    """
    out = copy.deepcopy(new_doc)
    if not prior_doc:
        return out
    prior = upgrade_legacy(prior_doc)

    disposed = [(c, member_comment_ids(c)) for c in prior["clusters"] if latest_disposition(c)]
    head_moved = prior.get("head") != out.get("head")
    matched = set()

    for cluster in out["clusters"]:
        ids = member_comment_ids(cluster)
        # sorted() is stable, so equal `at` keeps prior document order: the first wins.
        matches = sorted((c for c, prior_ids in disposed if ids & prior_ids), key=_at, reverse=True)
        if not matches:
            continue
        winner, losers = matches[0], matches[1:]
        matched.update(id(c) for c in matches)

        cluster["disposition"] = copy.deepcopy(latest_disposition(winner))
        history = copy.deepcopy(winner.get("disposition_history") or [])
        for loser in reversed(losers):  # oldest first
            history += copy.deepcopy(loser.get("disposition_history") or [])
            history.append(copy.deepcopy(latest_disposition(loser)))
        cluster["disposition_history"] = history
        if "escalation" in winner:
            cluster["escalation"] = copy.deepcopy(winner["escalation"])
        if _is_stale(cluster, head_moved, changed_files):
            cluster["disposition"]["stale"] = True

    out.setdefault("orphaned_dispositions", []).extend(
        {
            "cluster_id": c["cluster_id"],
            "member_comment_ids": sorted(prior_ids),
            "disposition": copy.deepcopy(latest_disposition(c)),
        }
        for c, prior_ids in disposed if id(c) not in matched
    )

    if not (out.get("timeline") or {}).get("plan_at") and prior.get("timeline"):
        out["timeline"] = copy.deepcopy(prior["timeline"])
    return out
