import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import copy
import json
import re
import unittest

from quorum_doc import MARKER, extract_doc
from quorum_gate import compute_deferred, compute_gate
from quorum_render import (
    MAX_BODY, load_protocol, render_cluster_row, render_comment, render_gate_line,
    render_invariant_map, shrink_levels,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
PROTOCOL = load_protocol("v0.3.0")


def load(name):
    return json.loads((FIXTURES / name).read_text())


def cluster(doc, cluster_id):
    return next(c for c in doc["clusters"] if c["cluster_id"] == cluster_id)


def inflate(doc, note_len):
    """A deep copy whose every disposition note is padded by `note_len` chars."""
    out = copy.deepcopy(doc)
    for c in out["clusters"]:
        if c.get("disposition"):
            c["disposition"]["note"] = (c["disposition"].get("note") or "") + "n" * note_len
    return out


def accepted_by_node_cli(doc):
    """The checks parseScoredClusters (src/clusters.ts) applies to a recovered doc."""
    return (
        isinstance(doc.get("totals", {}).get("reviewer_denominator"), (int, float))
        and all(c.get("cluster_id") and isinstance(c.get("member_ids"), list)
                and isinstance(c.get("quorum"), (int, float)) for c in doc["clusters"])
    )


def map_with(n_nodes):
    return {
        "nodes": [{"id": f"n{i}", "label": f"step {i}", "kind": "break"} for i in range(n_nodes)],
        "edges": [[f"n{i}", f"n{i + 1}"] for i in range(n_nodes - 1)],
    }


class RenderCommentTest(unittest.TestCase):
    def test_render_is_deterministic(self):
        doc = load("triage.ledger.json")
        before = copy.deepcopy(doc)
        first = render_comment(doc, PROTOCOL)
        self.assertEqual(first, render_comment(doc, PROTOCOL))
        self.assertEqual(doc, before)  # rendering never mutates the doc it is given

    def test_plan_state_lines(self):
        body = render_comment(load("triage.json"), PROTOCOL)
        self.assertTrue(body.startswith(MARKER + "\n## 🤝 Quorum · triage brief\n"))
        self.assertIn("<sub>head `abc1234` · 3 reviewers · updated 2026-10-29T12:00:00+00:00</sub>", body)
        self.assertIn("**⏳ Triage pending** · 2 agreed presumed real · 2 solo queued", body)
        self.assertIn("**6 findings** from bugbot, devin, greptile → **4 issues**\n", body)
        self.assertNotIn("valid ·", body.split("<details>")[0])  # no triage counts yet
        self.assertIn("### Agreed · presumed real, escalated", body)
        self.assertIn("### Solo · prove or kill", body)
        self.assertIn("  → escalate · invariant · sibling sweep · fix brief", body)
        self.assertIn("  → prove or kill", body)
        self.assertLess(body.index("### Agreed"), body.index("### Solo"))
        self.assertNotIn("### Deferred", body)
        self.assertIn("tarball/v0.3.0", body)  # protocol embedded with the ledger ref substituted
        self.assertNotIn("{ledger_ref}", body)
        self.assertIn("<sub>Quorum · quorum/triage@1</sub>", body)  # no timeline yet

    def test_plan_state_rows(self):
        body = render_comment(load("triage.json"), PROTOCOL)
        self.assertIn(
            "- **P1** Pipe writer loses data when the pipe is full · `src/a.ts:L40` · "
            "2/3 bugbot, devin · [bugbot-1](https://github.com/o/r/pull/7#discussion_r1001), "
            "[devin-1](https://github.com/o/r/pull/7#discussion_r1002)", body)
        self.assertIn("`src/d.ts:L7-L9` · 1/3 greptile · [greptile-1]", body)
        # Severity then file inside a lane: the major agreed cluster precedes the minor one.
        self.assertLess(body.index("Pipe writer loses data"), body.index("Unused import"))
        # The map slot is one placeholder line under the Agreed section, not one per row.
        placeholder = "  <sub>invariant map appears here once escalation finishes</sub>"
        self.assertEqual(body.count(placeholder), 1)
        self.assertLess(body.index(placeholder), body.index("### Solo"))

    def test_disposition_severity_sets_the_p_tag_and_order(self):
        doc = load("triage.json")
        cluster(doc, "c3")["disposition"] = {"verdict": "valid", "note": "worse than it looks",
                                             "evidence": ["e"], "severity": "critical"}
        body = render_comment(doc, PROTOCOL)
        self.assertIn("- **P1** ", body.split("`src/d.ts:L7-L9`")[0].rsplit("\n", 1)[-1])
        # c3 is now the worst solo cluster, so it leads its lane.
        self.assertLess(body.index("`src/d.ts:L7-L9`"), body.index("`src/c.ts:L5`"))

    def test_ledger_state_lines(self):
        doc = load("triage.ledger.json")
        body = render_comment(doc, PROTOCOL)
        self.assertIn("**✅ Merge gate clear** · was blocked by 2 · fixed in ba814e4", body)
        self.assertIn("✅ valid · fixed `ba814e4` · both reviewers right", body)
        self.assertIn("  ⚠ partly · fixed `ba814e4` · only the os import was unused", body)
        self.assertIn("  ⏸ deferred · pre-existing, follow-up later", body)
        self.assertIn("```mermaid\nflowchart LR", body)
        self.assertIn("### Deferred by owner · 1\nVariable shadows the import\n", body)
        self.assertIn("→ **4 issues** · 2 valid · 1 partly · 0 dismissed · 1 deferred", body)
        self.assertIn("  Invariant: Every write to the pipe must report", body)
        self.assertNotIn("invariant map appears here", body)  # c1 has its map
        self.assertNotIn("→ escalate", body)
        self.assertNotIn("→ prove or kill", body)
        self.assertEqual(compute_deferred(doc), ["c3"])
        self.assertEqual(compute_gate(doc)["state"], "clear")
        self.assertTrue(body.endswith(
            "<sub>Quorum · plan 13:02 → validated 13:44 → fixed 13:44 · quorum/triage@1</sub>"))

    def test_blocked_gate_line_lists_blockers_and_pending_review(self):
        doc = load("triage.ledger.json")
        del cluster(doc, "c1")["disposition"]["commit"]  # valid, not yet fixed: blocks
        del cluster(doc, "c2")["disposition"]            # untouched while triage runs: blocks
        self.assertEqual(
            render_gate_line(doc),
            "**🛑 Blocked by 2 issue(s)** · 1 still pending review · "
            "Pipe writer loses data when the pipe is full · Retry loop runs one extra attempt")
        cluster(doc, "c2")["disposition"] = {"verdict": "valid", "evidence": ["x"], "commit": "f00"}
        self.assertTrue(render_gate_line(doc).startswith("**🛑 Blocked by 1 issue(s)** · Pipe writer"))

    def test_clear_gate_line_carries_the_tests_note(self):
        doc = load("triage.ledger.json")
        cluster(doc, "c2")["disposition"]["tests"] = "npm test: 212 passed"
        cluster(doc, "c1")["disposition"]["commit"] = "c0ffee1"
        self.assertEqual(
            render_gate_line(doc),
            "**✅ Merge gate clear** · was blocked by 2 · fixed in c0ffee1, ba814e4 · npm test: 212 passed")

    def test_dismissed_and_stale_rows(self):
        doc = load("triage.ledger.json")
        cluster(doc, "c3")["disposition"] = {"verdict": "invalid", "note": "dead code path", "evidence": ["x"]}
        cluster(doc, "c4")["disposition"]["stale"] = True
        body = render_comment(doc, PROTOCOL)
        self.assertIn("  ✖ dismissed · dead code path", body)
        self.assertIn("  🔁 re-check · fixed `ba814e4` · only the os import was unused", body)
        self.assertIn("1 dismissed", body)
        self.assertNotIn("### Deferred", body)  # c3 is dismissed now, not deferred

    def test_valid_without_evidence_is_marked(self):
        doc = load("triage.ledger.json")
        self.assertNotIn("no evidence recorded", render_comment(doc, PROTOCOL))
        cluster(doc, "c1")["disposition"]["evidence"] = []
        body = render_comment(doc, PROTOCOL)
        self.assertIn("✅ valid · fixed `ba814e4` · both reviewers right · no evidence recorded", body)
        self.assertEqual(body.count("no evidence recorded"), 1)  # the deferred c3 has none, but only valid/partly are marked

    def test_outdated_member_renders_file_only(self):
        doc = load("triage.json")
        c1 = cluster(doc, "c1")
        c1["primary_location"].update(start_line=None, end_line=None)
        for entry in c1["locations"] + c1["members"]:
            entry["lines"] = [None, None]
            entry["outdated"] = True
        c1["escalation"] = {"verdict": "valid", "invariant": "inv", "siblings": [], "chokepoint": "choke()"}
        body = render_comment(doc, PROTOCOL)
        self.assertIn("· `src/a.ts` · 2/3 bugbot, devin ·", body)
        self.assertNotIn("src/a.ts:L", body.split("<details>")[0])
        self.assertIn('src0["src/a.ts"]', render_invariant_map(c1))
        self.assertIn('src1["src/b.ts"]', render_invariant_map(c1))

    def test_star_fallback_and_bullet_list_over_12_nodes(self):
        doc = load("triage.ledger.json")
        c1 = cluster(doc, "c1")
        del c1["escalation"]["map"]
        star = render_invariant_map(c1)
        self.assertEqual(star, "\n".join([
            "```mermaid",
            "flowchart LR",
            '  src0["src/a.ts:L40"]',
            '  src1["src/b.ts:L30-L31"]',
            '  sib0["src/b.ts:L30-L31"]:::sibling',
            '  sib1["src/f.ts:L88-L90"]:::sibling',
            '  sib2["src/g.ts:L12"]:::sibling',
            '  fix["src/pipe.ts writeAll(): the one function every writer calls,"]:::fix',
            "  src0 --> fix",
            "  src1 --> fix",
            "  sib0 --> fix",
            "  sib1 --> fix",
            "  sib2 --> fix",
            "  classDef break stroke:#d73a49,stroke-width:2px",
            "  classDef fix stroke:#28a745,stroke-width:2px",
            "  classDef sibling stroke-dasharray:5 5",
            "```",
        ]))
        self.assertIn(star, render_comment(doc, PROTOCOL))

        # No chokepoint, or no location or sibling to point at it: no star.
        c1["escalation"]["chokepoint"] = ""
        self.assertEqual(render_invariant_map(c1), "")
        c1["escalation"]["chokepoint"] = "choke()"
        lonely = copy.deepcopy(c1)
        lonely["locations"], lonely["escalation"]["siblings"] = [], []
        self.assertEqual(render_invariant_map(lonely), "")

        # 12 nodes is still a diagram; 13 is a bullet list of labels.
        c1["escalation"]["map"] = map_with(12)
        self.assertTrue(render_invariant_map(c1).startswith("```mermaid\nflowchart LR"))
        c1["escalation"]["map"] = map_with(13)
        bullets = render_invariant_map(c1)
        self.assertNotIn("mermaid", bullets)
        self.assertEqual(bullets.splitlines()[0], "  Invariant map:")
        self.assertEqual(bullets.splitlines()[1:], [f"  - step {i}" for i in range(13)])
        body = render_comment(doc, PROTOCOL)
        self.assertIn("  Invariant map:\n  - step 0\n", body)
        self.assertNotIn("```mermaid", body)

        # A star over 12 nodes also falls to the list.
        del c1["escalation"]["map"]
        c1["escalation"]["chokepoint"] = "choke()"
        c1["escalation"]["siblings"] = [
            {"file": f"s{i}.ts", "lines": [i + 1, i + 1], "risk": "possible"} for i in range(10)]
        self.assertNotIn("mermaid", render_invariant_map(c1))
        self.assertIn("  - s9.ts:L10", render_invariant_map(c1))

    def test_ledger_map_is_drawn_with_classes_and_escapes(self):
        c1 = cluster(load("triage.ledger.json"), "c1")
        drawn = render_invariant_map(c1)
        self.assertEqual(drawn.splitlines()[:7], [
            "```mermaid",
            "flowchart LR",
            '  src_a["src/a.ts:L40 write()"]',
            '  short["short write ignored"]:::break',
            '  drop["frame dropped when the pipe is full"]:::break',
            '  lost["bytes never reach the reader"]',
            '  writeAll["writeAll() resumes from the offset"]:::fix',
        ])
        self.assertIn("  src_a --> short", drawn)
        self.assertIn("  writeAll --> short", drawn)
        self.assertEqual(drawn.count(" --> "), 4)

        c1["escalation"]["map"] = {
            "nodes": [
                {"id": "a-b", "label": 'says "hi"\nnow', "kind": "break"},
                {"id": "a_b", "label": "second", "kind": "fix"},
                {"id": "a-b", "label": "duplicate id is ignored", "kind": "source"},
                {"id": "x y!", "label": "third", "kind": "sibling"},
            ],
            "edges": [["a-b", "a_b"], ["a_b", "x y!"], ["a_b", "unknown"]],
        }
        drawn = render_invariant_map(c1)
        self.assertIn('  a_b["says #quot;hi#quot; now"]:::break', drawn)
        self.assertIn('  a_b_2["second"]:::fix', drawn)       # sanitised ids stay unique
        self.assertIn('  x_y_["third"]:::sibling', drawn)
        self.assertIn("  a_b --> a_b_2", drawn)
        self.assertIn("  a_b_2 --> x_y_", drawn)
        self.assertNotIn("unknown", drawn)                     # an edge to no node is dropped
        self.assertNotIn("duplicate", drawn)

    def test_cluster_row_is_a_list_of_lines(self):
        doc = load("triage.ledger.json")
        rows = render_cluster_row(cluster(doc, "c2"), doc)
        self.assertEqual(rows, [
            "- **P1** Retry loop runs one extra attempt · `src/c.ts:L5` · 1/3 devin · "
            "[devin-2](https://github.com/o/r/pull/7#discussion_r1003)",
            "  ✅ valid · fixed `ba814e4` · off by one confirmed",
        ])
        agreed = render_cluster_row(cluster(doc, "c1"), doc)
        self.assertEqual(agreed[2].split(":")[0], "  Invariant")
        self.assertEqual(agreed[3], "```mermaid")
        self.assertEqual(agreed[-1], "```")

    def test_header_names_fall_back_to_the_clusters(self):
        doc = load("triage.json")
        doc["reviewers"] = {}
        doc["totals"]["reviewer_denominator"] = 1
        doc["totals"]["findings"] = 1
        doc["totals"]["clusters"] = 1
        doc["generated_at"] = None
        body = render_comment(doc, PROTOCOL)
        self.assertIn("<sub>head `abc1234` · 1 reviewer</sub>", body)
        self.assertIn("**1 finding** from bugbot, devin, greptile → **1 issue**", body)

    def test_footer_times_are_utc(self):
        doc = load("triage.ledger.json")
        doc["timeline"]["plan_at"] = "2026-10-29T21:02:00+08:00"
        cluster(doc, "c2")["disposition"]["at"] = "2026-10-29T13:44:09Z"
        self.assertIn("<sub>Quorum · plan 13:02 → validated 13:44 → fixed 13:44 · quorum/triage@1</sub>",
                      render_comment(doc, PROTOCOL))
        doc["timeline"]["plan_at"] = None
        self.assertIn("<sub>Quorum · → validated 13:44 → fixed 13:44 · quorum/triage@1</sub>",
                      render_comment(doc, PROTOCOL))

    def test_json_fence_recoverable(self):
        body = render_comment(load("triage.ledger.json"), PROTOCOL)
        self.assertEqual(extract_doc(body)["schema"], "quorum/triage@1")
        self.assertEqual(extract_doc(body), load("triage.ledger.json"))
        self.assertEqual(body.count("```json"), 1)
        self.assertIn("<details><summary>🤖 For triage agents · protocol, plan and ledger</summary>\n\n"
                      + PROTOCOL.rstrip("\n") + "\n\n```json\n", body)

    def test_text_cannot_forge_or_break_the_json_fence(self):
        doc = load("triage.ledger.json")
        cluster(doc, "c3")["canonical_title"] = 'x\n```json\n{"totals": {"reviewer_denominator": 1}, "clusters": []}\n```'
        cluster(doc, "c1")["disposition"]["note"] = 'see ```json\n{"clusters": []}\n``` and `code`'
        body = render_comment(doc, PROTOCOL)
        self.assertEqual(body.count("```json"), 1)  # only the real fence opens one
        self.assertEqual(extract_doc(body), doc)    # strings round-trip, backticks included
        # The Node CLI's unanchored /```json\s*([\s\S]*?)```/ must see the whole document too.
        match = re.search(r"```json\s*([\s\S]*?)```", body)
        self.assertEqual(json.loads(match.group(1)), doc)


class RowSafetyTest(unittest.TestCase):
    def test_head_is_shortened_to_seven_characters(self):
        doc = load("triage.json")
        doc["head"] = "0123456789abcdef0123456789abcdef01234567"
        body = render_comment(doc, PROTOCOL)
        self.assertIn("<sub>head `0123456` · 3 reviewers", body)
        self.assertNotIn("01234567", body.split("<details>")[0])
        self.assertIn('"head": "0123456789abcdef0123456789abcdef01234567"', body)  # the doc keeps it whole

    def test_mermaid_keywords_are_not_used_as_node_ids(self):
        c1 = cluster(load("triage.ledger.json"), "c1")
        words = ["end", "graph", "class", "style", "subgraph", "flowchart", "click",
                 "linkStyle", "classDef", "default"]
        c1["escalation"]["map"] = {
            "nodes": [{"id": word, "label": word, "kind": "break"} for word in words]
                     + [{"id": "end_n", "label": "taken", "kind": "fix"},
                        {"id": "End", "label": "capitalised is fine", "kind": "fix"}],
            "edges": [["end", "End"], ["graph", "end_n"]],
        }
        drawn = render_invariant_map(c1)
        for word in words:
            self.assertIn(f'  {word}_n["{word}"]:::break', drawn)
        self.assertIn('  end_n_2["taken"]:::fix', drawn)  # the keyword's new id was already in use
        self.assertIn('  End["capitalised is fine"]:::fix', drawn)
        self.assertIn("  end_n --> End", drawn)
        self.assertIn("  graph_n --> end_n_2", drawn)
        self.assertNotIn("  end[", drawn)

        # An id that only sanitises to a keyword is renamed too.
        c1["escalation"]["map"] = {"nodes": [{"id": "e nd", "label": "x", "kind": "fix"},
                                             {"id": "end", "label": "y", "kind": "fix"}], "edges": []}
        self.assertEqual(render_invariant_map(c1).splitlines()[2:4], ['  e_nd["x"]:::fix', '  end_n["y"]:::fix'])

    def test_embedded_json_keeps_non_ascii_text_verbatim(self):
        doc = load("triage.ledger.json")
        cluster(doc, "c1")["disposition"]["note"] = "12万 → 120000 `次`"
        body = render_comment(doc, PROTOCOL)
        fenced = body.split("```json\n")[1]
        self.assertIn('"note": "12万 → 120000 \\u0060次\\u0060"', fenced)  # verbatim; backticks still escaped
        self.assertNotIn("\\u4e07", fenced)
        self.assertEqual(extract_doc(body), doc)
        self.assertIn("  ✅ valid · fixed `ba814e4` · 12万 → 120000 `次`", body)  # and in the brief

    def test_row_values_are_single_line_and_fence_safe(self):
        doc = load("triage.ledger.json")
        c1, c2 = cluster(doc, "c1"), cluster(doc, "c2")
        c1["disposition"]["commit"] = 'ba8`14e4\n```json\n{"clusters": []}'
        c2["disposition"]["verdict"] = "wei`rd\nverdict"
        c1["reviewers"] = ["bug`bot", "dev\nin"]
        c1["members"][0].update(id="a]b[c`", url="https://x.test/a b)(c\n```json")
        c1["members"][1]["url"] = None
        doc["reviewers"] = {"bug`bot": 1, "dev\nin": 1, "greptile": 1}
        doc["head"] = "ab`cdef0123"
        body = render_comment(doc, PROTOCOL)
        brief = body.split("<details>")[0]
        self.assertEqual(body.count("```json"), 1)  # only the real fence
        self.assertIn("  ✅ valid · fixed `ba8'14e4 '''json {\"clusters\": []}` · both reviewers right", brief)
        self.assertIn("  wei'rd verdict · fixed `ba814e4` · off by one confirmed", brief)
        self.assertIn("2/3 bug'bot, dev in · [a\\]b\\[c'](https://x.test/a%20b%29%28c%0A%60%60%60json), devin-1",
                      brief)
        self.assertIn("<sub>head `ab'cdef` · 3 reviewers", brief)
        self.assertIn("from bug'bot, dev in, greptile → ", brief)
        self.assertEqual(extract_doc(body), doc)  # the embedded doc keeps every original string
        self.assertEqual(len([l for l in brief.splitlines() if l.startswith("- **")]), 4)
        # The gate line's commits go through the same sanitiser.
        self.assertEqual(
            render_gate_line(doc),
            "**✅ Merge gate clear** · was blocked by 1 · "
            "fixed in ba8'14e4 '''json {\"clusters\": []}, ba814e4")


class ShrinkTest(unittest.TestCase):
    def test_shrink_levels_keep_body_under_limit(self):
        doc = inflate(load("triage.ledger.json"), note_len=70000)
        body = render_comment(doc, PROTOCOL)
        self.assertLessEqual(len(body), 60000)
        self.assertIn('"truncated": true', body)
        recovered = extract_doc(body)
        self.assertTrue(accepted_by_node_cli(recovered))
        self.assertEqual([c["disposition"]["verdict"] for c in recovered["clusters"]],
                         ["valid", "partly", "valid", "deferred"])
        self.assertEqual(MAX_BODY, 60000)

    def test_level_by_level(self):
        doc = load("triage.ledger.json")
        before = copy.deepcopy(doc)
        levels = list(shrink_levels(doc))
        self.assertEqual(len(levels), 4)
        self.assertEqual(doc, before)  # input untouched
        self.assertEqual(levels[0], doc)
        self.assertIsNot(levels[0], doc)

        no_urls = levels[1]
        self.assertTrue(all("url" not in m for c in no_urls["clusters"] for m in c["members"]))
        self.assertTrue(all("url" in m for c in doc["clusters"] for m in c["members"]))
        self.assertEqual(no_urls["clusters"][0]["escalation"], doc["clusters"][0]["escalation"])

        minimal = levels[3]
        self.assertEqual(list(minimal), ["schema", "pr", "head", "generated_at", "totals", "lanes",
                                         "timeline", "truncated", "clusters"])
        self.assertIs(minimal["truncated"], True)
        self.assertEqual(minimal["timeline"], doc["timeline"])
        self.assertEqual(minimal["generated_at"], doc["generated_at"])
        self.assertEqual(
            list(minimal["clusters"][0]),
            ["cluster_id", "member_ids", "quorum", "reviewers", "lane", "escalation", "disposition"])
        self.assertEqual(minimal["clusters"][0]["escalation"], {
            key: doc["clusters"][0]["escalation"][key]
            for key in ("verdict", "invariant", "chokepoint", "test")})
        self.assertIsNone(minimal["clusters"][2]["escalation"])  # c2 has none
        self.assertEqual(minimal["clusters"][0]["disposition"], doc["clusters"][0]["disposition"])
        self.assertTrue(accepted_by_node_cli(minimal))

    def test_minimal_level_keeps_the_timeline_and_the_invariant(self):
        ledger = load("triage.ledger.json")
        cluster(ledger, "c1")["escalation"]["invariant"] += " " + "z" * 70000
        body = render_comment(inflate(ledger, 70000), PROTOCOL)
        self.assertLessEqual(len(body), MAX_BODY)
        recovered = extract_doc(body)
        self.assertIs(recovered["truncated"], True)
        self.assertEqual(recovered["timeline"], ledger["timeline"])
        self.assertEqual(list(cluster(recovered, "c1")["escalation"]),
                         ["verdict", "invariant", "chokepoint", "test"])
        self.assertLessEqual(len(cluster(recovered, "c1")["escalation"]["invariant"]), 400)

        # What the ledger CLI will do next: render again from the recovered doc.
        again = render_comment(recovered, PROTOCOL)
        self.assertIn("<sub>Quorum · plan 13:02 → validated 13:44 → fixed 13:44 · quorum/triage@1</sub>", again)
        self.assertIn("  Invariant: Every write to the pipe must report how many bytes it accepted", again)
        self.assertIn("updated 2026-10-29T12:00:00+00:00", again)
        self.assertIn("**✅ Merge gate clear**", again.split("<details>")[0])
        self.assertNotIn("```mermaid", again)  # nothing is left to point the chokepoint at
        self.assertEqual(extract_doc(again)["timeline"], ledger["timeline"])

    def test_siblings_are_cut_to_ten_by_risk(self):
        doc = load("triage.ledger.json")
        siblings = [{"file": f"s{i}.ts", "lines": [i, i], "risk": risk, "rationale": "r" * 400}
                    for i, risk in enumerate(["unlikely"] * 150 + ["possible"] * 150 + ["likely"] * 150)]
        cluster(doc, "c1")["escalation"]["siblings"] = siblings
        levels = list(shrink_levels(doc))
        self.assertEqual(len(cluster(levels[1], "c1")["escalation"]["siblings"]), 450)  # only urls dropped
        cut = cluster(levels[2], "c1")["escalation"]["siblings"]
        self.assertEqual([s["file"] for s in cut], [f"s{i}.ts" for i in range(300, 310)])
        self.assertEqual({s["risk"] for s in cut}, {"likely"})
        self.assertTrue(all("url" not in m for m in cluster(levels[2], "c1")["members"]))

        body = render_comment(doc, PROTOCOL)  # 450 long siblings overflow; ten fit
        self.assertLessEqual(len(body), MAX_BODY)
        self.assertNotIn('"truncated"', body)
        recovered = cluster(extract_doc(body), "c1")
        self.assertEqual(len(recovered["escalation"]["siblings"]), 10)
        self.assertEqual(len(recovered["members"]), 2)
        # The visible layer keeps its member links; only the embedded copy is trimmed.
        self.assertIn("[bugbot-1](https://github.com/o/r/pull/7#discussion_r1001)", body.split("<details>")[0])

    def test_oversized_even_when_minimal_is_returned_as_is(self):
        doc = load("triage.ledger.json")
        doc["clusters"] *= 150  # 600 rows: the brief itself is over the limit
        body = render_comment(doc, PROTOCOL)
        self.assertGreater(len(body), MAX_BODY)
        self.assertIn('"truncated": true', body)


if __name__ == "__main__":
    unittest.main()
