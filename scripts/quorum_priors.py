#!/usr/bin/env python3
"""quorum_priors.py — per-reviewer precision priors from past Quorum ledgers.

usage: quorum_priors.py OWNER/REPO [--out PATH]

Reads the trusted `quorum/triage@1` ledger comment of every pull request in the
repository, tallies what each reviewer's findings were judged to be, and writes
{"generated_at", "repo", "reviewers": {name: {valid, partly, invalid, n}}} to
PATH (default .quorum/priors/<owner>-<repo>.json at the git top level of the
current directory, or in it outside a git repository). `quorum_ledger.py
post-plan` embeds a fresh file in the next plan, so the agent sees how often
each reviewer has been right in this repository.

A prior is a prior, never a verdict: it says nothing about the finding in
front of you. A reviewer with fewer than MIN_N judged findings gets no prior,
and a file older than MAX_AGE_DAYS is ignored.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from typing import Iterable

import quorum_github as gh
from quorum_doc import MARKER, extract_doc, latest_disposition, now_iso

MIN_N = 5            # judged findings a reviewer needs before it gets a prior
MAX_AGE_DAYS = 7     # a priors file older than this is not embedded
OUTCOMES = ("valid", "partly", "invalid")


def repo_root() -> str:
    """The git top level of the current directory, or the current directory
    when it is not in a git repository (or git is missing). Best-effort."""
    try:
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return os.getcwd()
    top = res.stdout.strip()
    return top if res.returncode == 0 and top else os.getcwd()


def default_path(repo: str) -> str:
    """Where `main` writes and `post-plan` reads the priors for `repo`:
    `.quorum/priors/<owner>-<repo>.json` under `repo_root()`, so a run from any
    directory of the checkout finds the same cache."""
    return os.path.join(repo_root(), ".quorum", "priors", f"{repo.replace('/', '-')}.json")


# --- aggregating -------------------------------------------------------------

def _judgements(doc):
    """(reviewer, verdict) for every member of every judged cluster in `doc`.

    A member's verdict is its own entry in the disposition's `members` map when
    there is one, else the cluster's verdict. `deferred` is no judgement, so a
    deferred cluster or member counts for nothing. A stale disposition still
    counts: priors are history. Orphaned dispositions name no reviewer.

    A truncated ledger keeps each member's id but not its reviewer; finding ids
    are `<reviewer>-<n>` (normalize_findings.jq), so the reviewer is read off
    the id there.
    """
    for cluster in doc.get("clusters") or []:
        disposition = latest_disposition(cluster)
        if not isinstance(disposition, dict):
            continue
        overrides = disposition.get("members")
        overrides = overrides if isinstance(overrides, dict) else {}
        for member in cluster.get("members") or []:
            reviewer = member.get("reviewer") or str(member.get("id") or "").rpartition("-")[0]
            verdict = overrides.get(member.get("id"), disposition.get("verdict"))
            if reviewer and verdict in OUTCOMES:
                yield reviewer, verdict


def aggregate(docs: Iterable[dict]) -> dict:
    """{reviewer: {"valid", "partly", "invalid": fraction, "n": count}} over `docs`,
    leaving out reviewers with fewer than MIN_N judged findings."""
    tallies: dict = {}
    for doc in docs:
        for reviewer, verdict in _judgements(doc):
            tally = tallies.setdefault(reviewer, dict.fromkeys(OUTCOMES, 0))
            tally[verdict] += 1
    priors = {}
    for reviewer, tally in sorted(tallies.items()):
        n = sum(tally.values())
        if n >= MIN_N:
            priors[reviewer] = {**{o: round(tally[o] / n, 3) for o in OUTCOMES}, "n": n}
    return priors


# --- collecting --------------------------------------------------------------

def collect_docs(client: gh.Client, repo: str) -> list[dict]:
    """The ledger document of every pull request (open or closed) in `repo` that
    has a trusted, parseable Quorum comment. One lookup per pull request."""
    pulls = client.api("GET", f"repos/{repo}/pulls",
                       params={"state": "all", "paginate": True}) or []
    login = gh.get_authenticated_login(client)
    docs = []
    for pull in pulls:
        found = gh.find_marker_comment(client, repo, pull["number"], MARKER, login=login)
        doc = extract_doc(found["body"]) if found else None
        if doc is not None:
            docs.append(doc)
    return docs


# --- reading back ------------------------------------------------------------

def load_fresh_priors(path: str, now: datetime.datetime) -> dict:
    """The `reviewers` map of the priors file at `path`, or {} when the file is
    missing, unreadable, malformed, undated or more than MAX_AGE_DAYS from `now`."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        age = now - datetime.datetime.fromisoformat(data["generated_at"])
        reviewers = data["reviewers"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if abs(age) > datetime.timedelta(days=MAX_AGE_DAYS) or not isinstance(reviewers, dict):
        return {}
    return reviewers


# --- command line ------------------------------------------------------------

def summary(reviewer: str, prior: dict) -> str:
    shares = ", ".join(f"{o} {prior[o]:.2f}" for o in OUTCOMES)
    return f"{reviewer}: {shares} (n={prior['n']})"


def parser():
    ap = argparse.ArgumentParser(prog="quorum_priors.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", metavar="OWNER/REPO")
    ap.add_argument("--out", metavar="PATH",
                    help="where to write the priors (default .quorum/priors/<owner>-<repo>.json "
                         "at the repository's top level)")
    return ap


def main(argv: list[str] | None = None, client: gh.Client | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as e:  # argparse has printed the usage error (2) or the help (0)
        return e.code if isinstance(e.code, int) else 2
    try:
        docs = collect_docs(gh.default_client() if client is None else client, args.repo)
        reviewers = aggregate(docs)
        path = args.out or default_path(args.repo)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": now_iso(), "repo": args.repo, "reviewers": reviewers},
                      f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception as e:  # a failed read or write: report it, never a traceback
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    for reviewer, prior in reviewers.items():
        print(summary(reviewer, prior))
    if not docs:
        print(f"no Quorum ledgers found in {args.repo}; no priors yet")
    elif not reviewers:
        print(f"no reviewer has {MIN_N} judged findings yet across {len(docs)} ledger(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
