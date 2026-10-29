"""quorum_gate.py — the merge gate, the deferred list and the timeline, all
derived from the dispositions in a `quorum/triage@1` document.

Pure functions over the document. A `stale` disposition (the head moved and
touched the cluster's files) counts as no disposition throughout.
"""

from quorum_doc import latest_disposition

# Only these severities can hold the gate; agreed minor/nit clusters never do.
GATE_RELEVANT = {"critical", "major"}
# Verdicts that block until a fixing commit is recorded.
OPEN_VERDICTS = {"valid", "partly"}


def effective_severity(cluster):
    """The severity the owner recorded on the latest disposition, else the cluster's own."""
    return (latest_disposition(cluster) or {}).get("severity") or cluster.get("severity")


def live_disposition(cluster):
    """The cluster's latest disposition, or None when it has none or it is stale."""
    d = latest_disposition(cluster)
    return d if d and not d.get("stale") else None


def is_disposed(cluster):
    return live_disposition(cluster) is not None


def triage_started(doc):
    """Triage has started once any cluster carries a live disposition."""
    return any(is_disposed(c) for c in doc["clusters"])


def blocks(cluster, started):
    """Whether `cluster` holds the merge gate. `started` is `triage_started(doc)`."""
    if effective_severity(cluster) not in GATE_RELEVANT:
        return False
    d = live_disposition(cluster)
    if d is None:
        return started
    return d.get("verdict") in OPEN_VERDICTS and not d.get("commit")


def compute_gate(doc):
    """{state, blocking, pending_review, was_blocked_by, fixed_commits, tests}.

    `state` is "pending" before any cluster is disposed, "blocked" while any
    cluster blocks, else "clear". `pending_review` is the blocking subset that
    has no live disposition. `was_blocked_by` counts the gate-relevant clusters
    the owner confirmed (valid or partly), fixed or not. `fixed_commits` lists
    each commit once, in document order; `tests` is the note on the most
    recently recorded disposition that has one.
    """
    clusters = doc["clusters"]
    started = triage_started(doc)
    blocking = [c["cluster_id"] for c in clusters if blocks(c, started)]
    pending_review = [c["cluster_id"] for c in clusters if blocks(c, started) and not is_disposed(c)]
    live = [d for d in map(live_disposition, clusters) if d]
    confirmed = [c for c in clusters
                 if effective_severity(c) in GATE_RELEVANT
                 and (live_disposition(c) or {}).get("verdict") in OPEN_VERDICTS]

    fixed_commits = []
    for d in live:
        if d.get("commit") and d["commit"] not in fixed_commits:
            fixed_commits.append(d["commit"])
    # Most recent test note by `at`; sorted() is stable, so a tie goes to the
    # cluster later in the document. (Document order alone is quorum/severity
    # order, not time order.)
    tested = sorted((d for d in live if d.get("tests")), key=lambda d: d.get("at") or "")

    if not started:
        state = "pending"
    elif blocking:
        state = "blocked"
    else:
        state = "clear"
    return {
        "state": state,
        "blocking": blocking,
        "pending_review": pending_review,
        "was_blocked_by": len(confirmed),
        "fixed_commits": fixed_commits,
        "tests": tested[-1]["tests"] if tested else None,
    }


def compute_deferred(doc):
    """Ids of the clusters the owner deferred, at any severity."""
    return [c["cluster_id"] for c in doc["clusters"]
            if (live_disposition(c) or {}).get("verdict") == "deferred"]


def compute_timeline(doc, previous):
    """{plan_at, first_disposition_at, last_disposition_at, fixed_at}.

    `plan_at` is kept from the document, else carried from `previous`. The rest
    span every disposition ever recorded (latest and history, stale included);
    `fixed_at` is the latest one that carries a commit. Timestamps are
    now_iso() strings, so they compare lexicographically; absent ones are None.
    """
    plan_at = (doc.get("timeline") or {}).get("plan_at")
    if plan_at is None and previous:
        plan_at = (previous.get("timeline") or {}).get("plan_at")

    dispositions = []
    for c in doc["clusters"]:
        dispositions += c.get("disposition_history") or []
        if latest_disposition(c):
            dispositions.append(latest_disposition(c))
    stamps = [d["at"] for d in dispositions if d.get("at")]
    fixed = [d["at"] for d in dispositions if d.get("at") and d.get("commit")]
    return {
        "plan_at": plan_at,
        "first_disposition_at": min(stamps) if stamps else None,
        "last_disposition_at": max(stamps) if stamps else None,
        "fixed_at": max(fixed) if fixed else None,
    }
