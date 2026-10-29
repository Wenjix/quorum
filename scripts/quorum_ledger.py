#!/usr/bin/env python3
"""quorum_ledger.py — the one script that writes the Quorum PR comment.

Every write re-renders the comment from the `quorum/triage@1` document, after
recomputing its `gate`, `deferred` and `timeline`, so the markdown is always a
function of the embedded JSON.

usage: quorum_ledger.py post-plan OWNER/REPO PR clusters.scored.json
                        [--dry-run] [--minimize] [--no-reactions]
       quorum_ledger.py dispose OWNER/REPO PR [CLUSTER_ID] --verdict V --note N
                        [--evidence E]... [--member ID:V]... [--severity S]
                        [--commit SHA] [--tests T] [--by NAME] [--reply]
                        [--from FILE] [--scored PATH] [--dry-run]
       quorum_ledger.py escalate OWNER/REPO PR CLUSTER_ID --from FILE
                        [--by NAME] [--scored PATH] [--dry-run]
       quorum_ledger.py show OWNER/REPO PR

post-plan builds from the fresh scored file and inherits dispositions from the
posted comment; unless the scored file already carries `priors`, it embeds the
repository's cached ones (`quorum_priors.py`, ignored once a week old). dispose
and escalate change the posted document (a truncated one is rebuilt from
--scored first). An escalation file may carry `members`
({member_id: verdict}, checked as --member is) for the disposition it records.
Reactions, minimising and replies are best-effort: a failure is counted, never
fatal. --dry-run prints the body to stdout and the planned side effects to
stderr, and writes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys

import quorum_github as gh
from quorum_doc import MARKER, extract_doc, latest_disposition, now_iso, upgrade_legacy
from quorum_gate import compute_deferred, compute_gate, compute_timeline
from quorum_merge import inherit
from quorum_priors import default_path, load_fresh_priors
from quorum_render import MAX_BODY, load_protocol, render_comment

LEDGER_REF = "v0.3.0"
VERDICTS = ("valid", "partly", "invalid", "deferred")
SEVERITIES = ("critical", "major", "minor", "nit")
REPLY_LABELS = {"valid": "valid", "partly": "partly", "invalid": "dismissed", "deferred": "deferred"}
TEXT_LIMIT = 2000         # each note, tests note and evidence item
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
COMPARE_FILE_CAP = 300    # GitHub's compare lists at most 300 files: a full list is unknown
REFRESH_KEYS = ("commit", "tests")
DISPOSITION_FLAGS = ("verdict", "note", "evidence", "member", "severity", "commit", "tests")
ESCALATION_TEXT = ("invariant", "chokepoint", "test")
DEFAULT_BY = "agent"


class LedgerError(Exception):
    """A refusal: the message goes to stderr and `code` is the exit status."""

    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


# --- reading -----------------------------------------------------------------

def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise LedgerError(f"cannot read {path}: {e}")


def find_ledger(client, repo, pr, tolerate=False):
    """(comment id, embedded doc) of the trusted ledger comment, or (None, None).
    With `tolerate` (a dry run) a failed read is reported and treated as none."""
    try:
        found = gh.find_marker_comment(client, repo, pr, MARKER) if client is not None else None
    except Exception as e:
        if not tolerate:
            raise
        print(f"note: could not read the PR comments ({e}); planning a new comment", file=sys.stderr)
        return None, None
    return (found["id"], extract_doc(found["body"])) if found else (None, None)


def changed_files(client, repo, prior_head, new_head):
    """Files changed between the heads, [] when they match, None when unknown."""
    if prior_head == new_head:
        return []
    if not (client and prior_head and new_head):
        return None
    files = gh.compare_files(client, repo, prior_head, new_head)
    return None if files is None or len(files) >= COMPARE_FILE_CAP else files


def without_null_escalations(doc):
    for c in doc["clusters"]:
        if "escalation" in c and c["escalation"] is None:
            del c["escalation"]
    return doc


def posted_ledger(client, args):
    """(comment id, full doc, posted doc) for dispose and escalate."""
    comment_id, posted = find_ledger(client, args.repo, args.pr)
    if posted is None:
        raise LedgerError(f"no Quorum ledger comment on {args.repo}#{args.pr}; run post-plan first", 1)
    if not posted.get("truncated"):
        return comment_id, without_null_escalations(upgrade_legacy(posted)), posted
    if not args.scored:
        raise LedgerError("the posted ledger is truncated; pass --scored PATH "
                          "(the local clusters.scored.json) to rebuild it")
    scored = upgrade_legacy(load_json(args.scored))
    files = changed_files(client, args.repo, posted.get("head"), scored.get("head"))
    doc = inherit(scored, posted, files)
    return comment_id, without_null_escalations(doc), posted


# --- writing -----------------------------------------------------------------

def finalize(doc, previous):
    """Store the derived fields the brief is rendered from, then render."""
    doc["gate"] = compute_gate(doc)
    doc["deferred"] = compute_deferred(doc)
    doc["timeline"] = compute_timeline(doc, previous)
    return render_comment(doc, load_protocol(LEDGER_REF))


def best_effort(label, attempts):
    """Run each zero-argument call in `attempts`; print how many succeeded."""
    ok = sum(1 for attempt in attempts if attempt())
    print(f"{label}: {ok}/{len(attempts)}")


def write(client, args, body, comment_id, effects):
    """Upsert the comment, then run `effects` ({label: [(description, call)]})."""
    if args.dry_run:
        print(body)
        print("--- planned side effects (dry run) ---", file=sys.stderr)
        target = f"update comment {comment_id}" if comment_id else "post new comment"
        print(f"{target} on {args.repo}#{args.pr} ({len(body)} characters)", file=sys.stderr)
        for label, items in effects.items():
            print(f"{label} {len(items)}: {', '.join(d for d, _ in items)}", file=sys.stderr)
        return 0
    try:
        written = gh.upsert_comment(client, args.repo, args.pr, body, comment_id)
    except Exception as e:
        size = f"{len(body)} characters" + (f", over the {MAX_BODY} limit" if len(body) > MAX_BODY else "")
        raise LedgerError(f"GitHub rejected the Quorum comment ({size}): {e}", 1)
    print(f"{'updated' if comment_id else 'posted'} Quorum comment {written}")
    for label, items in effects.items():
        best_effort(label, [call for _, call in items])
    return 0


# --- post-plan -----------------------------------------------------------------

def cmd_post_plan(args, client):
    doc = upgrade_legacy(load_json(args.scored))
    comment_id, prior = find_ledger(client, args.repo, args.pr, tolerate=args.dry_run)
    if prior:
        doc = inherit(doc, prior, changed_files(client, args.repo, prior.get("head"), doc.get("head")))
        without_null_escalations(doc)
        recorded = sum(1 for c in prior.get("clusters") or [] if latest_disposition(c))
        if recorded and not doc.get("orphaned_dispositions") and not any(
                latest_disposition(c) for c in doc["clusters"]):
            print(f"warning: none of the {recorded} dispositions in the posted ledger carried to "
                  "this plan or was kept as an orphan", file=sys.stderr)
    doc["timeline"] = dict(doc.get("timeline") or {})
    doc["timeline"]["plan_at"] = doc["timeline"].get("plan_at") or now_iso()
    if not doc.get("priors"):  # priors embedded by validate_partition.py --priors win
        doc["priors"] = load_fresh_priors(default_path(args.repo), datetime.datetime.now(datetime.timezone.utc))
    body = finalize(doc, prior)

    effects = {}
    if not args.no_reactions:
        effects["reactions"] = [
            (str(m["comment_id"]), lambda m=m: gh.add_eyes(client, args.repo, m["comment_id"]))
            for c in doc["clusters"] if c.get("lane") == "agreed"
            for m in c.get("members") or [] if m.get("comment_id") is not None]
    if args.minimize:
        effects["minimize"] = [
            (m["id"], lambda m=m: gh.minimize(client, m["node_id"]))
            for c in doc["clusters"] if len(c.get("members") or []) > 1
            for m in c["members"][1:] if m.get("node_id")]
    return write(client, args, body, comment_id, effects)


# --- dispose and escalate -------------------------------------------------------

def check_text(label, value):
    if value is not None and len(str(value)) > TEXT_LIMIT:
        raise LedgerError(f"{label} is longer than {TEXT_LIMIT} characters")
    return value


def check_choice(label, value, choices):
    if value is not None and value not in choices:
        raise LedgerError(f"{label} must be one of {'|'.join(choices)}, not {value!r}")
    return value


def cluster_by_id(doc, cluster_id):
    for c in doc["clusters"]:
        if c["cluster_id"] == cluster_id:
            return c
    raise LedgerError(f"unknown cluster_id: {cluster_id}")


def parse_members(pairs, cluster):
    """`ID:verdict` pairs as {id: verdict}, each id a member of `cluster`."""
    if pairs is None or isinstance(pairs, dict):
        pairs = [f"{k}:{v}" for k, v in (pairs or {}).items()]
    out = {}
    for pair in pairs:
        member_id, sep, verdict = str(pair).rpartition(":")
        if not sep or member_id not in (cluster.get("member_ids") or []):
            raise LedgerError(f"--member {pair}: expected ID:verdict with an ID of {cluster['cluster_id']}")
        out[member_id] = check_choice(f"--member {pair}", verdict, VERDICTS)
    return out


def build_disposition(entry, cluster, by, at):
    """The new latest disposition `entry` (flag or file keys) describes."""
    evidence = entry.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    for item in evidence:
        check_text("each --evidence", item)
    if entry.get("commit") is not None and not COMMIT_RE.match(str(entry["commit"])):
        raise LedgerError(f"--commit {entry['commit']!r} is not a 7 to 40 character lowercase hex SHA")
    check_text("--tests", entry.get("tests"))
    check_text("--note", entry.get("note"))
    check_text("mechanism_correction", entry.get("mechanism_correction"))
    severity = check_choice("--severity", entry.get("severity"), SEVERITIES)
    members = parse_members(entry.get("members"), cluster)

    verdict = check_choice("--verdict", entry.get("verdict"), VERDICTS)
    prior = latest_disposition(cluster)
    if verdict is None:
        refresh = {k: entry[k] for k in REFRESH_KEYS if entry.get(k) is not None}
        extra = [k for k in ("note", "severity", "mechanism_correction") if entry.get(k)]
        if not prior or not refresh or extra or evidence or members:
            raise LedgerError(f"{cluster['cluster_id']}: --verdict is required unless the call only adds "
                              "--commit and/or --tests to an existing disposition")
        kept = {k: v for k, v in prior.items() if k not in ("at", "stale")}
        return {**kept, **refresh, "by": by or prior.get("by") or DEFAULT_BY, "at": at}
    if not entry.get("note"):
        raise LedgerError(f"{cluster['cluster_id']}: a new verdict needs --note")
    out = {"verdict": verdict, "note": entry["note"], "evidence": list(evidence), "by": by or DEFAULT_BY, "at": at}
    for key, value in (("members", members), ("severity", severity), ("commit", entry.get("commit")),
                       ("tests", entry.get("tests")),
                       ("mechanism_correction", entry.get("mechanism_correction"))):
        if value:
            out[key] = value
    return out


def record(cluster, disposition):
    """Make `disposition` the latest, pushing the previous one onto the history."""
    previous = latest_disposition(cluster)
    if previous:
        cluster["disposition_history"] = (cluster.get("disposition_history") or []) + [previous]
    cluster["disposition"] = disposition


def reply_effects(client, args, changed):
    """One planned thread reply per member of each changed cluster."""
    items = []
    for cluster in changed:
        d = cluster["disposition"]
        fixed = f" · fixed in `{d['commit']}`" if d.get("commit") else ""
        text = f"**Quorum** · {REPLY_LABELS.get(d['verdict'], d['verdict'])}{fixed} · {d['note']}"
        for m in cluster.get("members") or []:
            if m.get("comment_id") is not None:
                items.append((str(m["comment_id"]), lambda m=m, text=text: gh.reply_in_thread(
                    client, args.repo, args.pr, m["comment_id"], text)))
    return {"replies": items}


def dispose_entries(args):
    """The dispositions this call records, as dicts with flag-named keys."""
    if args.from_file:
        mixed = [f"--{flag}" for flag in DISPOSITION_FLAGS if getattr(args, flag) is not None]
        if mixed:
            raise LedgerError(f"--from FILE cannot be combined with {', '.join(mixed)}")
        data = load_json(args.from_file)
        entries = data.get("dispositions") if isinstance(data, dict) and "dispositions" in data else [data]
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise LedgerError(f"{args.from_file}: expected one object or {{\"dispositions\": [...]}}")
        return [{**{k: v for k, v in e.items() if k != "QUORUM_DISPOSITION"},
                 "cluster_id": e.get("cluster_id") or args.cluster_id} for e in entries]
    if not args.cluster_id:
        raise LedgerError("dispose needs CLUSTER_ID or --from FILE")
    return [{"cluster_id": args.cluster_id, "verdict": args.verdict, "note": args.note,
             "evidence": args.evidence, "members": args.member, "severity": args.severity,
             "commit": args.commit, "tests": args.tests}]


def cmd_dispose(args, client):
    entries = dispose_entries(args)
    comment_id, doc, posted = posted_ledger(client, args)
    at, changed = now_iso(), {}
    for entry in entries:
        cluster = cluster_by_id(doc, entry.get("cluster_id"))
        record(cluster, build_disposition(entry, cluster, args.by, at))
        changed[cluster["cluster_id"]] = cluster  # listed twice: one reply, the last entry wins
    changed = list(changed.values())
    for cluster in changed:
        d = cluster["disposition"]
        if d["verdict"] in ("valid", "partly") and not d.get("evidence"):
            print(f"warning: {cluster['cluster_id']}: {d['verdict']} recorded without --evidence", file=sys.stderr)
    body = finalize(doc, posted)
    return write(client, args, body, comment_id, reply_effects(client, args, changed) if args.reply else {})


def cmd_escalate(args, client):
    escalation = load_json(args.from_file)
    if not isinstance(escalation, dict):
        raise LedgerError(f"{args.from_file}: expected an escalation object")
    escalation.pop("QUORUM_ESCALATION", None)
    if not escalation.get("invariant"):
        raise LedgerError(f"{args.from_file}: an escalation needs an invariant")
    for key in ESCALATION_TEXT:
        check_text(f"the escalation's {key}", escalation.get(key))
    comment_id, doc, posted = posted_ledger(client, args)
    cluster = cluster_by_id(doc, args.cluster_id)
    if cluster.get("lane") != "agreed":
        raise LedgerError(f"{args.cluster_id} is in the {cluster.get('lane')!r} lane; escalate takes agreed "
                          "clusters only (prove or kill a solo cluster with dispose)")
    at, by = now_iso(), args.by or DEFAULT_BY
    entry = {"verdict": escalation.get("verdict") or "valid", "note": escalation.get("invariant"),
             "members": escalation.get("members")}
    disposition = build_disposition(entry, cluster, by, at)
    disposition["evidence"] = escalation.get("evidence") or []
    cluster["escalation"] = {**escalation, "by": by, "at": at}
    record(cluster, disposition)
    return write(client, args, finalize(doc, posted), comment_id, {})


def cmd_show(args, client):
    _, doc = find_ledger(client, args.repo, args.pr)
    if doc is None:
        raise LedgerError(f"no Quorum ledger comment on {args.repo}#{args.pr}", 1)
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return 0


# --- command line ------------------------------------------------------------

def parser():
    ap = argparse.ArgumentParser(prog="quorum_ledger.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def target(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("repo", metavar="OWNER/REPO")
        p.add_argument("pr", metavar="PR")
        return p

    plan = target("post-plan", "post or refresh the triage brief from clusters.scored.json")
    plan.add_argument("scored", metavar="clusters.scored.json")
    plan.add_argument("--dry-run", action="store_true")
    plan.add_argument("--minimize", action="store_true",
                      help="collapse non-primary members of multi-member clusters (opt-in)")
    plan.add_argument("--no-reactions", action="store_true")

    dispose = target("dispose", "record a verdict on a cluster")
    dispose.add_argument("cluster_id", metavar="CLUSTER_ID", nargs="?")
    dispose.add_argument("--verdict", choices=VERDICTS)
    dispose.add_argument("--note")
    dispose.add_argument("--evidence", action="append")
    dispose.add_argument("--member", action="append", metavar="ID:VERDICT")
    dispose.add_argument("--severity", choices=SEVERITIES)
    dispose.add_argument("--commit", metavar="SHA")
    dispose.add_argument("--tests")
    dispose.add_argument("--reply", action="store_true", help="reply in every member's thread")
    dispose.add_argument("--from", dest="from_file", metavar="FILE")

    escalate = target("escalate", "record an agreed cluster's escalation")
    escalate.add_argument("cluster_id", metavar="CLUSTER_ID")
    escalate.add_argument("--from", dest="from_file", metavar="FILE", required=True)

    for p in (dispose, escalate):
        p.add_argument("--by", help=f"who recorded it (default {DEFAULT_BY!r}; a --commit/--tests "
                                     "refresh keeps the prior author)")
        p.add_argument("--scored", metavar="PATH",
                       help="the local clusters.scored.json, needed when the posted ledger is truncated")
        p.add_argument("--dry-run", action="store_true")

    target("show", "print the embedded JSON")
    return ap


COMMANDS = {"post-plan": cmd_post_plan, "dispose": cmd_dispose, "escalate": cmd_escalate, "show": cmd_show}


def main(argv: list[str] | None = None, client: gh.Client | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as e:  # argparse has printed the usage error (2) or the help (0)
        return e.code if isinstance(e.code, int) else 2
    try:
        if client is None:
            try:
                client = gh.default_client()
            except RuntimeError:
                if not (args.command == "post-plan" and args.dry_run):
                    raise  # only a planning dry run can go on without GitHub
        return COMMANDS[args.command](args, client)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except Exception as e:  # a failed read or write: report it, never a traceback
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
