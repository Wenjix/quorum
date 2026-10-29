"""quorum_render.py — render the two-layer synthesis comment.

The top layer is the human brief: header, merge gate, counts, one row per
cluster grouped by lane, the owner's deferrals. Under it, a collapsed
<details> block is the agent layer: the protocol and the `quorum/triage@1`
document as a fenced json block.

Pure: the only I/O is `load_protocol` reading the protocol reference. The same
document and protocol text always render the identical string, and the
document is never mutated. The gate, deferred list and timeline are computed
here from the dispositions, not read from the document.
"""

import copy
import datetime
import json
import pathlib
import re

from quorum_doc import MARKER
from quorum_gate import (
    OPEN_VERDICTS, compute_deferred, compute_gate, compute_timeline, effective_severity,
    live_disposition, triage_started,
)
from validate_partition import P_TAG, SCHEMA, SEVERITIES, lane_for

MAX_BODY = 60000  # GitHub's comment hard limit is 65536
PROTOCOL_PATH = pathlib.Path(__file__).resolve().parent.parent / "references" / "triage-protocol.md"

MAX_MAP_NODES = 12     # a bigger invariant map is a bullet list, not a diagram
STAR_LABEL_CHARS = 60  # the chokepoint label on the star fallback
NOTE_LIMIT = 400       # one note or invariant in the brief; also the minimal embedded note
SIBLING_LIMIT = 10     # siblings kept by the third shrink level
RISK_ORDER = {"likely": 0, "possible": 1, "unlikely": 2}
HEAD_CHARS = 7         # the head SHA as shown in the header
ESCALATION_KEYS = ("verdict", "invariant", "chokepoint", "test")  # what the minimal doc keeps
# Words that break a mermaid flowchart when used as a node id.
MERMAID_RESERVED = {"end", "graph", "class", "style", "subgraph", "flowchart", "click",
                    "linkStyle", "classDef", "default"}

PENDING_LINES = {
    "agreed": "→ escalate · invariant · sibling sweep · fix brief",
    "solo": "→ prove or kill",
}
VERDICT_LABELS = {
    "valid": "✅ valid", "partly": "⚠ partly", "invalid": "✖ dismissed", "deferred": "⏸ deferred",
}
STALE_LABEL = "🔁 re-check"
MAP_PLACEHOLDER = "  <sub>invariant map appears here once escalation finishes</sub>"
# The diagram classes; a node of any other kind (source, symptom) is left plain.
CLASS_DEFS = (
    "classDef break stroke:#d73a49,stroke-width:2px",
    "classDef fix stroke:#28a745,stroke-width:2px",
    "classDef sibling stroke-dasharray:5 5",
)
CLASSED_KINDS = {"break", "fix", "sibling"}


def load_protocol(ledger_ref):
    """The protocol text with `{ledger_ref}` replaced by the tag, branch or SHA
    the agent should fetch the scripts from."""
    return PROTOCOL_PATH.read_text(encoding="utf-8").replace("{ledger_ref}", ledger_ref)


# --- text helpers -----------------------------------------------------------

def one_line(text, limit=None):
    """`text` on a single line, cut to `limit` characters. Collapsing newlines
    keeps agent- and reviewer-written text from breaking the row it sits in, and
    swapping a triple backtick keeps it from opening a fenced block that the
    Node CLI's unanchored json-fence scan could take for the document."""
    flat = " ".join(str(text).split()).replace("```", "'''")
    if limit is not None and len(flat) > limit:
        return flat[:limit - 1].rstrip() + "…"
    return flat


def plain(text, limit=None):
    """`one_line` for a value that is not prose (an id, a name, a SHA, a verdict):
    backticks go too, so it can sit inside a code span or a link without
    breaking the row."""
    return one_line(text, limit).replace("`", "'")


def link_url(url):
    """`url` as a markdown link destination: whitespace, parentheses, angle
    brackets and backticks percent-encoded; "" when there is no url."""
    if not url:
        return ""
    return re.sub(r"[\s()<>`]", lambda m: f"%{ord(m.group()):02X}", str(url))


def plural(count, noun):
    return f"{count} {noun}" + ("" if count == 1 else "s")


def format_location(file, lines):
    """`file:L10-L12`, `file:L10`, or the bare file when the anchor is outdated
    and `lines` is None or [None, None]."""
    start, end = (list(lines or []) + [None, None])[:2]
    if start and end and start != end:
        return f"{file}:L{start}-L{end}"
    if start or end:
        return f"{file}:L{start or end}"
    return str(file)


def format_clock(iso):
    """`HH:MM` in UTC for an ISO timestamp (naive means UTC), or None when it is
    absent or does not parse."""
    if not iso:
        return None
    text = str(iso)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"  # fromisoformat takes no "Z" before Python 3.11
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime("%H:%M")


# --- cluster accessors ------------------------------------------------------

def lane_of(cluster):
    """"agreed" or "solo"; any other lane value is shown as solo."""
    lane = cluster.get("lane") or lane_for(cluster.get("quorum") or 0)
    return lane if lane in PENDING_LINES else "solo"


def title_of(cluster):
    return one_line(cluster.get("canonical_title") or cluster.get("cluster_id") or "?")


def p_tag_of(cluster):
    """The P-tag of the effective severity: a severity the owner recorded on a
    live disposition replaces the cluster's own tag."""
    severity = effective_severity(cluster)
    if severity != cluster.get("severity"):
        return plain(P_TAG.get(severity) or "—")
    return plain(cluster.get("p_tag") or P_TAG.get(severity) or "—")


def cluster_location(cluster):
    """The row's anchor: the primary location, else the first location or member
    that names a file."""
    primary = cluster.get("primary_location") or {}
    if primary.get("file"):
        return format_location(primary["file"], [primary.get("start_line"), primary.get("end_line")])
    for entry in (cluster.get("locations") or []) + (cluster.get("members") or []):
        if entry.get("file"):
            return format_location(entry["file"], entry.get("lines"))
    return "?"


def member_link(member):
    label = plain(member.get("id")).replace("[", "\\[").replace("]", "\\]")
    url = link_url(member.get("url"))
    return f"[{label}]({url})" if url else label


def member_links(cluster):
    members = cluster.get("members") or [{"id": i} for i in cluster.get("member_ids") or []]
    return ", ".join(member_link(m) for m in members)


def reviewer_names(doc):
    """Sorted keys of `doc["reviewers"]`, else every reviewer any cluster names."""
    names = set(doc.get("reviewers") or {})
    if not names:
        names = {r for c in doc["clusters"] for r in c.get("reviewers") or []}
    return sorted(plain(name) for name in names)


def lane_clusters(doc, lane):
    """The clusters in `lane`, by effective severity (worst first), then file.
    sorted() is stable, so ties keep document order."""
    in_lane = [c for c in doc["clusters"] if lane_of(c) == lane]
    return sorted(in_lane, key=lambda c: (-SEVERITIES.get(effective_severity(c), -1),
                                          (c.get("primary_location") or {}).get("file") or ""))


# --- invariant map ----------------------------------------------------------

def map_nodes_and_edges(cluster):
    """([(id, label, kind)], [(from, to)]) for the cluster's invariant map: the
    agent's `escalation.map`, else a star of every location and sibling pointing
    at the chokepoint. Empty when there is nothing to draw."""
    escalation = cluster.get("escalation") or {}
    drawn = escalation.get("map") or {}
    if drawn.get("nodes"):
        nodes = [(n.get("id"), n.get("label"), n.get("kind")) for n in drawn["nodes"]]
        edges = [tuple(e) for e in drawn.get("edges") or [] if len(e) == 2]
        return nodes, edges

    chokepoint = one_line(escalation.get("chokepoint") or "")[:STAR_LABEL_CHARS]
    if not chokepoint:
        return [], []
    locations = [loc for loc in cluster.get("locations") or [] if loc.get("file")]
    siblings = [sib for sib in escalation.get("siblings") or [] if sib.get("file")]
    nodes = [(f"src{i}", format_location(loc["file"], loc.get("lines")), "source")
             for i, loc in enumerate(locations)]
    nodes += [(f"sib{i}", format_location(sib["file"], sib.get("lines")), "sibling")
              for i, sib in enumerate(siblings)]
    if not nodes:
        return [], []
    edges = [(node_id, "fix") for node_id, _, _ in nodes]
    return nodes + [("fix", chokepoint, "fix")], edges


def mermaid_ids(raw_ids):
    """raw id -> an id of [A-Za-z0-9_] only, never a mermaid keyword (`end`
    becomes `end_n`). Two raw ids that sanitise alike stay distinct (`a-b` and
    `a_b` become `a_b` and `a_b_2`)."""
    taken, ids = set(), {}
    for raw in raw_ids:
        base = re.sub(r"[^A-Za-z0-9_]", "_", str(raw)) or "_"
        if base in MERMAID_RESERVED:
            base += "_n"
        candidate, n = base, 2
        while candidate in taken:
            candidate, n = f"{base}_{n}", n + 1
        taken.add(candidate)
        ids[raw] = candidate
    return ids


def render_mermaid(nodes, edges):
    unique = {}  # the first node with a repeated id wins
    for node_id, label, kind in nodes:
        unique.setdefault(str(node_id), (label, kind))
    ids = mermaid_ids(unique)
    lines = ["```mermaid", "flowchart LR"]
    for raw, (label, kind) in unique.items():
        label = one_line(label if label is not None else raw).replace('"', "#quot;")
        lines.append(f'  {ids[raw]}["{label}"]' + (f":::{kind}" if kind in CLASSED_KINDS else ""))
    lines += [f"  {ids[str(a)]} --> {ids[str(b)]}" for a, b in edges if str(a) in ids and str(b) in ids]
    lines += [f"  {class_def}" for class_def in CLASS_DEFS]
    lines.append("```")
    return "\n".join(lines)


def render_invariant_map(cluster):
    """The cluster's invariant map: a mermaid block, or — past MAX_MAP_NODES — an
    indented bullet list of the labels under `Invariant map:`; "" when there is
    nothing to draw. The fence starts at column 0 (it ends the list item it
    follows), the bullet list nests under the row."""
    nodes, edges = map_nodes_and_edges(cluster)
    if not nodes:
        return ""
    if len(nodes) > MAX_MAP_NODES:
        labels = [one_line(label if label is not None else node_id) for node_id, label, _ in nodes]
        return "\n".join(["  Invariant map:"] + [f"  - {label}" for label in labels])
    return render_mermaid(nodes, edges)


# --- rows and sections ------------------------------------------------------

def render_disposition_line(cluster):
    """The row's second line: its verdict, or what is still to do."""
    d = cluster.get("disposition")
    if not d:
        return PENDING_LINES[lane_of(cluster)]
    stale = bool(d.get("stale"))
    parts = [STALE_LABEL if stale else VERDICT_LABELS.get(d.get("verdict"), plain(d.get("verdict")))]
    if d.get("commit"):
        parts.append(f"fixed `{plain(d['commit'])}`")
    if d.get("note"):
        parts.append(one_line(d["note"], NOTE_LIMIT))
    if not stale and d.get("verdict") in OPEN_VERDICTS and not d.get("evidence"):
        parts.append("no evidence recorded")
    return " · ".join(parts)


def render_cluster_row(cluster, doc):
    """The row's lines: the summary, the disposition and, for an escalated
    agreed cluster, the invariant and its map."""
    denominator = (doc.get("totals") or {}).get("reviewer_denominator")
    lines = [
        f"- **{p_tag_of(cluster)}** {title_of(cluster)} · `{plain(cluster_location(cluster))}` · "
        f"{plain(cluster.get('quorum'))}/{plain(denominator)} "
        f"{', '.join(plain(r) for r in cluster.get('reviewers') or [])} · {member_links(cluster)}",
        "  " + render_disposition_line(cluster),
    ]
    escalation = cluster.get("escalation")
    if lane_of(cluster) == "agreed" and escalation:
        if escalation.get("invariant"):
            lines.append(f"  Invariant: {one_line(escalation['invariant'], NOTE_LIMIT)}")
        lines += render_invariant_map(cluster).splitlines()
    return lines


def render_lane_section(doc, lane, heading):
    """The lane's heading and rows, or nothing when the lane is empty. While no
    agreed cluster has an escalation, one placeholder line holds the map's place."""
    clusters = lane_clusters(doc, lane)
    if not clusters:
        return []
    rows = [row for c in clusters for row in render_cluster_row(c, doc)]
    if lane == "agreed" and not any(c.get("escalation") for c in clusters):
        rows.append(MAP_PLACEHOLDER)
    return [heading, *rows, ""]


def render_deferred_section(doc):
    titles = {c["cluster_id"]: title_of(c) for c in doc["clusters"]}
    deferred = compute_deferred(doc)
    if not deferred:
        return []
    return [f"### Deferred by owner · {len(deferred)}", " · ".join(titles[i] for i in deferred), ""]


# --- header, gate, counts, footer -------------------------------------------

def render_header(doc):
    totals = doc.get("totals") or {}
    reviewers = totals.get("reviewer_denominator", len(reviewer_names(doc)))
    parts = ([f"head `{plain(str(doc['head'])[:HEAD_CHARS])}`"] if doc.get("head") else [])
    parts.append(plural(reviewers, "reviewer"))
    if doc.get("generated_at"):
        parts.append(f"updated {plain(doc['generated_at'])}")
    return ["## 🤝 Quorum · triage brief", f"<sub>{' · '.join(parts)}</sub>"]


def render_gate_line(doc):
    gate = compute_gate(doc)
    clusters = doc["clusters"]
    if gate["state"] == "pending":
        agreed = sum(1 for c in clusters if lane_of(c) == "agreed")
        return (f"**⏳ Triage pending** · {agreed} agreed presumed real · "
                f"{len(clusters) - agreed} solo queued")
    if gate["state"] == "blocked":
        parts = [f"**🛑 Blocked by {len(gate['blocking'])} issue(s)**"]
        if gate["pending_review"]:
            parts.append(f"{len(gate['pending_review'])} still pending review")
        titles = {c["cluster_id"]: title_of(c) for c in clusters}
        return " · ".join(parts + [titles[i] for i in gate["blocking"]])
    parts = ["**✅ Merge gate clear**"]
    if gate["was_blocked_by"]:
        parts.append(f"was blocked by {gate['was_blocked_by']}")
    if gate["fixed_commits"]:
        parts.append(f"fixed in {', '.join(plain(c) for c in gate['fixed_commits'])}")
    if gate["tests"]:
        parts.append(one_line(gate["tests"], NOTE_LIMIT))
    return " · ".join(parts)


def render_counts_line(doc):
    clusters = doc["clusters"]
    totals = doc.get("totals") or {}
    findings = totals.get("findings", sum(len(c.get("member_ids") or []) for c in clusters))
    issues = totals.get("clusters", len(clusters))
    line = (f"**{plural(findings, 'finding')}** from {', '.join(reviewer_names(doc)) or 'no reviewers'} "
            f"→ **{plural(issues, 'issue')}**")
    if triage_started(doc):
        verdicts = [(live_disposition(c) or {}).get("verdict") for c in clusters]
        line += (f" · {verdicts.count('valid')} valid · {verdicts.count('partly')} partly · "
                 f"{verdicts.count('invalid')} dismissed · {verdicts.count('deferred')} deferred")
    return line


def render_footer(doc):
    timeline = compute_timeline(doc, None)
    segments = []
    for prefix, key in (("plan", "plan_at"), ("→ validated", "last_disposition_at"),
                        ("→ fixed", "fixed_at")):
        clock = format_clock(timeline.get(key))
        if clock:
            segments.append(f"{prefix} {clock}")
    middle = [" ".join(segments)] if segments else []
    return f"<sub>{' · '.join(['Quorum', *middle, SCHEMA])}</sub>"


# --- the two layers ---------------------------------------------------------

def render_brief(doc):
    """The human layer, as lines, ending on the blank line before the agent layer."""
    return [
        MARKER, *render_header(doc), "",
        render_gate_line(doc), render_counts_line(doc), "",
        *render_lane_section(doc, "agreed", "### Agreed · presumed real, escalated"),
        *render_lane_section(doc, "solo", "### Solo · prove or kill"),
        *render_deferred_section(doc),
    ]


def render_agent_layer(protocol_text, embedded):
    """The collapsed block: the protocol, then the document in one json fence.
    A backtick in the json is escaped as \\u0060 (same string once parsed) so
    that no string, however it is written, can end the fence early."""
    return [
        "<details><summary>🤖 For triage agents · protocol, plan and ledger</summary>",
        "",
        protocol_text.rstrip("\n"),
        "",
        "```json",
        json.dumps(embedded, indent=2, ensure_ascii=False).replace("`", "\\u0060"),
        "```",
        "</details>",
    ]


# --- shrinking --------------------------------------------------------------

def cap_strings(value, limit):
    """`value` with every string in it cut to `limit` characters."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"
    if isinstance(value, list):
        return [cap_strings(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: cap_strings(item, limit) for key, item in value.items()}
    return value


def without_member_urls(doc):
    out = copy.deepcopy(doc)
    for c in out["clusters"]:
        for member in c.get("members") or []:
            member.pop("url", None)
    return out


def with_top_siblings(doc):
    """A copy whose escalations keep at most SIBLING_LIMIT siblings, likeliest first."""
    out = copy.deepcopy(doc)
    for c in out["clusters"]:
        siblings = (c.get("escalation") or {}).get("siblings") or []
        if len(siblings) > SIBLING_LIMIT:
            ranked = sorted(siblings, key=lambda s: RISK_ORDER.get(s.get("risk"), len(RISK_ORDER)))
            c["escalation"]["siblings"] = ranked[:SIBLING_LIMIT]
    return out


def minimal_escalation(escalation):
    """The escalation's verdict, invariant, chokepoint and test, cut to
    NOTE_LIMIT; None when the cluster has no escalation."""
    if not escalation:
        return None
    return cap_strings({key: escalation[key] for key in ESCALATION_KEYS if key in escalation}, NOTE_LIMIT)


def minimal_doc(doc):
    """What the Node CLI needs, the merge gate and deferred list (computed as
    the brief's gate line computes them, text cut to NOTE_LIMIT), the timeline,
    and per cluster its members reduced to {id, comment_id} (so a re-run can
    still match dispositions by comment id), its escalation (reduced) and its
    disposition (text cut to NOTE_LIMIT); marked `truncated`."""
    return {
        "schema": doc.get("schema"),
        "pr": doc.get("pr"),
        "head": doc.get("head"),
        "generated_at": doc.get("generated_at"),
        "totals": copy.deepcopy(doc.get("totals")),
        "lanes": copy.deepcopy(doc.get("lanes")),
        "gate": cap_strings(compute_gate(doc), NOTE_LIMIT),
        "deferred": compute_deferred(doc),
        "timeline": copy.deepcopy(doc.get("timeline")),
        "truncated": True,
        "clusters": [
            {
                "cluster_id": c.get("cluster_id"),
                "member_ids": list(c.get("member_ids") or []),
                "members": [{"id": m.get("id"), "comment_id": m.get("comment_id")}
                            for m in c.get("members") or []],
                "quorum": c.get("quorum"),
                "reviewers": list(c.get("reviewers") or []),
                "lane": lane_of(c),
                "escalation": minimal_escalation(c.get("escalation")),
                "disposition": cap_strings(c.get("disposition"), NOTE_LIMIT),
            }
            for c in doc["clusters"]
        ],
    }


def shrink_levels(doc):
    """The document as embedded, from complete to minimal: unchanged; without
    member urls; also with each escalation's siblings cut to the ten likeliest;
    then the minimal doc. Every level is a copy."""
    yield copy.deepcopy(doc)
    slim = without_member_urls(doc)
    yield slim
    yield with_top_siblings(slim)
    yield minimal_doc(doc)


def render_comment(doc, protocol_text):
    """The comment body: the brief rendered from `doc`, then the agent layer
    embedding the most complete level of `doc` that fits MAX_BODY. When even the
    minimal level does not fit, that one is returned anyway."""
    brief = render_brief(doc)
    footer = ["", render_footer(doc)]
    body = ""
    for level in shrink_levels(doc):
        body = "\n".join(brief + render_agent_layer(protocol_text, level) + footer)
        if len(body) <= MAX_BODY:
            break
    return body
