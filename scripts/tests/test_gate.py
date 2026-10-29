import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import copy
import datetime
import json
import unittest

from quorum_doc import (
    MARKER, SCHEMA, extract_doc, is_triage, latest_disposition, now_iso, upgrade_legacy,
)
from quorum_gate import (
    GATE_RELEVANT, blocks, compute_deferred, compute_gate, compute_timeline,
    effective_severity, is_disposed, triage_started,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# An old-format scored doc (no `schema`), the shape clusters.scored.json had
# before quorum/triage@1. Modeled on sampleScoredDoc() in test/helpers.ts.
LEGACY_SCORED = {
    "generated_at": "2026-06-12T00:00:00Z",
    "totals": {"findings": 3, "clusters": 2, "reviewer_denominator": 3, "gate_split": []},
    "clusters": [
        {
            "cluster_id": "c1",
            "member_ids": ["bugbot-1", "devin-1"],
            "canonical_title": "Shared default object is mutated",
            "canonical_description": "Object.assign mutates defaults and leaks options across calls.",
            "category": "logic",
            "severity": "major",
            "primary_location": {"file": "src/options.ts", "start_line": 12, "end_line": 14},
            "match_type": "same-root-cause",
            "match_confidence": 0.9,
            "cross_file": False,
            "quorum": 2,
            "reviewers": ["bugbot", "devin"],
            "members": [
                {"id": "bugbot-1", "reviewer": "bugbot", "file": "src/options.ts",
                 "lines": [12, 14], "url": "https://example.test/comment/1"},
                {"id": "devin-1", "reviewer": "devin", "file": "src/routes.ts",
                 "lines": [88, 88], "url": "https://example.test/comment/2"},
            ],
        },
        {
            "cluster_id": "c2",
            "member_ids": ["copilot-1"],
            "canonical_title": "Missing help text punctuation",
            "category": "style",
            "severity": "nit",
            "primary_location": {"file": "README.md", "start_line": 4, "end_line": 4},
            "match_type": "singleton",
            "match_confidence": 1,
            "cross_file": False,
            "quorum": 1,
            "reviewers": ["copilot"],
            "members": [
                {"id": "copilot-1", "reviewer": "copilot", "file": "README.md",
                 "lines": [4, 4], "url": "https://example.test/comment/3"},
            ],
        },
    ],
}

_CLOCK = [0]


def load(name):
    return json.loads((FIXTURES / name).read_text())


def with_disposition(doc, cid, verdict, note, commit=None, stale=False, tests=None):
    """Deep copy of `doc` with a disposition recorded on one cluster; every call
    stamps a later `at`, so call order is time order."""
    out = copy.deepcopy(doc)
    _CLOCK[0] += 1
    disposition = {"verdict": verdict, "note": note, "evidence": [], "by": "test",
                   "at": f"2026-10-29T10:{_CLOCK[0]:02d}:00+00:00"}
    if commit:
        disposition["commit"] = commit
    if tests:
        disposition["tests"] = tests
    if stale:
        disposition["stale"] = True
    next(c for c in out["clusters"] if c["cluster_id"] == cid)["disposition"] = disposition
    return out


def fence(payload):
    return "```json\n" + json.dumps(payload) + "\n```"


class FixtureTest(unittest.TestCase):
    def test_fixture_is_a_complete_triage_doc(self):
        doc = load("triage.json")
        self.assertTrue(is_triage(doc))
        self.assertEqual(doc["lanes"], {"agreed": ["c1", "c4"], "solo": ["c2", "c3"]})
        self.assertEqual({c["cluster_id"]: c["severity"] for c in doc["clusters"]},
                         {"c1": "major", "c2": "major", "c3": "minor", "c4": "minor"})
        self.assertEqual(doc["totals"]["reviewer_denominator"], 3)
        self.assertFalse(any("disposition" in c for c in doc["clusters"]))


class GateTest(unittest.TestCase):
    def test_pending_when_no_dispositions(self):
        g = compute_gate(load("triage.json"))
        self.assertEqual(g["state"], "pending")
        self.assertEqual(g["blocking"], [])

    def test_undisposed_major_blocks_once_started(self):
        doc = with_disposition(load("triage.json"), "c3", verdict="invalid", note="n")
        g = compute_gate(doc)
        self.assertEqual(g["state"], "blocked")
        self.assertEqual(sorted(g["blocking"]), ["c1", "c2"])
        self.assertEqual(sorted(g["pending_review"]), ["c1", "c2"])

    def test_valid_without_commit_blocks_and_with_commit_clears(self):
        doc = load("triage.json")
        for cid in ("c1", "c2"):
            doc = with_disposition(doc, cid, verdict="valid", note="n")
        self.assertEqual(compute_gate(doc)["state"], "blocked")
        for cid in ("c1", "c2"):
            doc = with_disposition(doc, cid, verdict="valid", note="n", commit="ba814e4")
        g = compute_gate(doc)
        self.assertEqual((g["state"], g["was_blocked_by"], g["fixed_commits"]), ("clear", 2, ["ba814e4"]))

    def test_deferred_and_minor_never_block(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="deferred", note="later")
        doc = with_disposition(doc, "c2", verdict="invalid", note="no")
        self.assertEqual(compute_gate(doc)["state"], "clear")
        self.assertEqual(compute_deferred(doc), ["c1"])

    def test_disposition_severity_overrides_the_cluster_severity(self):
        doc = with_disposition(load("triage.json"), "c3", verdict="valid", note="n")
        doc = with_disposition(doc, "c2", verdict="valid", note="n", commit="x")
        doc["clusters"][3]["disposition"]["severity"] = "critical"  # c3, minor on the cluster
        doc["clusters"][2]["disposition"]["severity"] = "minor"     # c2, major on the cluster
        self.assertEqual([effective_severity(c) for c in doc["clusters"]], ["major", "minor", "minor", "critical"])
        g = compute_gate(doc)
        self.assertEqual((g["blocking"], g["was_blocked_by"]), (["c1", "c3"], 1))

    def test_stale_disposition_counts_as_undisposed(self):
        doc = with_disposition(load("triage.json"), "c2", verdict="valid", note="n", commit="x", stale=True)
        doc = with_disposition(doc, "c1", verdict="valid", note="n", commit="y")
        self.assertIn("c2", compute_gate(doc)["pending_review"])

    def test_extract_doc_and_legacy_upgrade(self):
        body = "x\n```mermaid\nflowchart LR\n```\n```json\n" + json.dumps(LEGACY_SCORED) + "\n```"
        doc = upgrade_legacy(extract_doc(body))
        self.assertEqual(doc["schema"], "quorum/triage@1")
        self.assertEqual(doc["clusters"][0]["lane"], "agreed")

    def test_partly_without_commit_blocks_like_valid(self):
        doc = load("triage.json")
        doc = with_disposition(doc, "c1", verdict="partly", note="n")
        doc = with_disposition(doc, "c2", verdict="invalid", note="n")
        g = compute_gate(doc)
        self.assertEqual((g["state"], g["blocking"], g["pending_review"]), ("blocked", ["c1"], []))
        doc = with_disposition(doc, "c1", verdict="partly", note="n", commit="abc1234")
        g = compute_gate(doc)
        self.assertEqual((g["state"], g["blocking"], g["was_blocked_by"]), ("clear", [], 1))

    def test_clear_when_only_gate_relevant_clusters_are_disposed(self):
        # c3 (solo/minor) and c4 (agreed/minor) are never gate-relevant, so
        # leaving them untouched cannot hold the gate.
        doc = with_disposition(load("triage.json"), "c1", verdict="invalid", note="n")
        doc = with_disposition(doc, "c2", verdict="invalid", note="n")
        g = compute_gate(doc)
        self.assertEqual((g["state"], g["blocking"], g["pending_review"], g["was_blocked_by"]),
                         ("clear", [], [], 0))

    def test_was_blocked_by_ignores_minor_clusters(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="valid", note="n", commit="aaa1111")
        doc = with_disposition(doc, "c2", verdict="invalid", note="n")
        doc = with_disposition(doc, "c4", verdict="valid", note="n", commit="bbb2222")
        g = compute_gate(doc)
        self.assertEqual(g["was_blocked_by"], 1)
        self.assertEqual(g["fixed_commits"], ["aaa1111", "bbb2222"])  # commits still listed for minors

    def test_fixed_commits_are_unique_in_order_of_first_appearance(self):
        doc = load("triage.json")
        doc = with_disposition(doc, "c1", verdict="valid", note="n", commit="bbb2222")
        doc = with_disposition(doc, "c2", verdict="partly", note="n", commit="aaa1111")
        doc = with_disposition(doc, "c4", verdict="valid", note="n", commit="bbb2222")
        self.assertEqual(compute_gate(doc)["fixed_commits"], ["bbb2222", "aaa1111"])

    def test_tests_is_the_most_recent_non_empty_note(self):
        doc = load("triage.json")
        self.assertIsNone(compute_gate(doc)["tests"])
        doc = with_disposition(doc, "c1", verdict="valid", note="n", commit="aaa1111", tests="npm test: 12 pass")
        doc = with_disposition(doc, "c2", verdict="invalid", note="n")  # no tests: must not erase the earlier note
        self.assertEqual(compute_gate(doc)["tests"], "npm test: 12 pass")
        doc = with_disposition(doc, "c4", verdict="valid", note="n", commit="bbb2222", tests="pytest: 3 pass")
        self.assertEqual(compute_gate(doc)["tests"], "pytest: 3 pass")

    def test_tests_follows_time_not_document_order(self):
        # Fixture order is c1, c4, c2, c3. c2 is dispositioned first and c1 later,
        # so the newest note sits on the cluster that comes first in the document.
        doc = with_disposition(load("triage.json"), "c2", verdict="valid", note="n",
                               commit="aaa1111", tests="OLD 10 pass")
        doc = with_disposition(doc, "c1", verdict="valid", note="n", commit="bbb2222", tests="NEW 12 pass")
        self.assertEqual(compute_gate(doc)["tests"], "NEW 12 pass")
        # Reversing the document must not change which note is newest.
        doc["clusters"].reverse()
        self.assertEqual(compute_gate(doc)["tests"], "NEW 12 pass")
        # fixed_commits is deliberately document order (first appearance), not time.
        self.assertEqual(compute_gate(doc)["fixed_commits"], ["aaa1111", "bbb2222"])
        doc["clusters"].reverse()
        self.assertEqual(compute_gate(doc)["fixed_commits"], ["bbb2222", "aaa1111"])

    def test_tests_tie_on_at_goes_to_the_later_cluster_in_the_document(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="valid", note="n", tests="first")
        doc = with_disposition(doc, "c2", verdict="valid", note="n", tests="second")
        for c in doc["clusters"]:
            if "disposition" in c:
                c["disposition"]["at"] = "2026-10-29T10:00:00+00:00"
        self.assertEqual(compute_gate(doc)["tests"], "second")  # c2 follows c1 in the fixture
        doc["clusters"].reverse()
        self.assertEqual(compute_gate(doc)["tests"], "first")

    def test_tests_note_without_at_loses_to_one_with_it(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="valid", note="n", tests="dated")
        doc = with_disposition(doc, "c2", verdict="valid", note="n", tests="undated")
        del doc["clusters"][2]["disposition"]["at"]  # c2 is after c1 in the fixture
        self.assertEqual(compute_gate(doc)["tests"], "dated")

    def test_only_stale_dispositions_leave_the_gate_pending(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="valid", note="n", commit="x", stale=True)
        g = compute_gate(doc)
        self.assertEqual((g["state"], g["blocking"], g["pending_review"]), ("pending", [], []))

    def test_stale_disposition_does_not_count_toward_the_summary(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="valid", note="n", commit="ba814e4", stale=True)
        doc = with_disposition(doc, "c2", verdict="deferred", note="later", tests="stale note", stale=True)
        doc = with_disposition(doc, "c3", verdict="invalid", note="n")
        g = compute_gate(doc)
        self.assertEqual((g["was_blocked_by"], g["fixed_commits"], g["tests"]), (0, [], None))
        self.assertEqual(compute_deferred(doc), [])

    def test_blocking_follows_document_order(self):
        doc = with_disposition(load("triage.json"), "c3", verdict="invalid", note="n")
        self.assertEqual(compute_gate(doc)["blocking"], ["c1", "c2"])
        doc["clusters"].reverse()
        self.assertEqual(compute_gate(doc)["blocking"], ["c2", "c1"])


class GateRuleTest(unittest.TestCase):
    def test_gate_relevant_severities(self):
        self.assertEqual(GATE_RELEVANT, {"critical", "major"})

    def test_is_disposed(self):
        self.assertFalse(is_disposed({}))
        self.assertTrue(is_disposed({"disposition": {"verdict": "valid"}}))
        self.assertFalse(is_disposed({"disposition": {"verdict": "valid", "stale": True}}))

    def test_triage_started(self):
        doc = load("triage.json")
        self.assertFalse(triage_started(doc))
        self.assertTrue(triage_started(with_disposition(doc, "c4", verdict="invalid", note="n")))
        self.assertFalse(triage_started(
            with_disposition(doc, "c4", verdict="invalid", note="n", stale=True)))

    def test_blocks_truth_table(self):
        def cluster(severity, verdict=None, commit=None, stale=False):
            c = {"severity": severity}
            if verdict:
                c["disposition"] = {"verdict": verdict}
                if commit:
                    c["disposition"]["commit"] = commit
                if stale:
                    c["disposition"]["stale"] = True
            return c

        # undisposed: blocks only once triage has started, and only if gate-relevant
        self.assertTrue(blocks(cluster("critical"), started=True))
        self.assertFalse(blocks(cluster("critical"), started=False))
        self.assertFalse(blocks(cluster("minor"), started=True))
        self.assertFalse(blocks(cluster("nit"), started=True))
        # valid / partly block until a commit is recorded, regardless of `started`
        for verdict in ("valid", "partly"):
            self.assertTrue(blocks(cluster("major", verdict), started=False))
            self.assertFalse(blocks(cluster("major", verdict, commit="abc1234"), started=True))
            self.assertFalse(blocks(cluster("minor", verdict), started=True))
        # invalid and deferred never block
        for verdict in ("invalid", "deferred"):
            self.assertFalse(blocks(cluster("critical", verdict), started=True))
        # stale: treated as undisposed
        self.assertTrue(blocks(cluster("major", "invalid", stale=True), started=True))
        self.assertFalse(blocks(cluster("major", "invalid", stale=True), started=False))


class TimelineTest(unittest.TestCase):
    def test_empty_timeline_is_all_none(self):
        self.assertEqual(compute_timeline(load("triage.json"), None), {
            "plan_at": None, "first_disposition_at": None,
            "last_disposition_at": None, "fixed_at": None})

    def test_plan_at_is_kept_then_carried_from_previous(self):
        doc = load("triage.json")
        self.assertEqual(compute_timeline(doc, {"timeline": {"plan_at": "P0"}})["plan_at"], "P0")
        doc["timeline"] = {"plan_at": "P1"}
        self.assertEqual(compute_timeline(doc, {"timeline": {"plan_at": "P0"}})["plan_at"], "P1")
        self.assertEqual(compute_timeline(doc, None)["plan_at"], "P1")

    def test_previous_without_a_timeline_is_tolerated(self):
        self.assertIsNone(compute_timeline(load("triage.json"), {})["plan_at"])

    def test_first_last_and_fixed_span_latest_and_history(self):
        doc = load("triage.json")
        c1 = doc["clusters"][0]
        c1["disposition_history"] = [
            {"verdict": "valid", "at": "2026-10-29T09:00:00+00:00"},
            {"verdict": "valid", "commit": "old0000", "at": "2026-10-29T09:30:00+00:00"},
        ]
        c1["disposition"] = {"verdict": "invalid", "at": "2026-10-29T10:00:00+00:00"}
        c2 = doc["clusters"][2]
        c2["disposition"] = {"verdict": "partly", "commit": "abc1234", "at": "2026-10-29T09:45:00+00:00"}
        self.assertEqual(compute_timeline(doc, None), {
            "plan_at": None,
            "first_disposition_at": "2026-10-29T09:00:00+00:00",
            "last_disposition_at": "2026-10-29T10:00:00+00:00",
            "fixed_at": "2026-10-29T09:45:00+00:00",
        })

    def test_fixed_at_stays_none_without_a_commit(self):
        doc = with_disposition(load("triage.json"), "c1", verdict="valid", note="n")
        t = compute_timeline(doc, None)
        self.assertIsNotNone(t["first_disposition_at"])
        self.assertEqual(t["first_disposition_at"], t["last_disposition_at"])
        self.assertIsNone(t["fixed_at"])


class DocTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(SCHEMA, "quorum/triage@1")
        self.assertEqual(MARKER, "<!-- quorum:synthesis -->")

    def test_is_triage(self):
        self.assertTrue(is_triage({"schema": SCHEMA, "clusters": []}))
        self.assertFalse(is_triage(LEGACY_SCORED))
        self.assertFalse(is_triage({"schema": "quorum/triage@2"}))
        self.assertFalse(is_triage(None))

    def test_latest_disposition(self):
        self.assertIsNone(latest_disposition({"cluster_id": "c1"}))
        d = {"verdict": "valid"}
        self.assertIs(latest_disposition({"disposition": d}), d)

    def test_now_iso_is_utc_seconds_precision(self):
        stamp = now_iso()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        parsed = datetime.datetime.fromisoformat(stamp)
        self.assertLess(abs((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()), 60)

    def test_extract_doc_returns_the_last_json_block_with_clusters(self):
        first = {"clusters": [{"cluster_id": "old"}]}
        last = {"schema": SCHEMA, "clusters": [{"cluster_id": "new"}]}
        body = "\n".join([
            "intro", fence(first),
            "```json\nnot json at all\n```",
            fence({"unrelated": True}),
            fence({"clusters": "not a list"}),
            fence(last),
            fence([1, 2]),
            "```json\n```",
        ])
        self.assertEqual(extract_doc(body), last)

    def test_extract_doc_handles_crlf_and_pretty_printed_json(self):
        doc = {"schema": SCHEMA, "clusters": [{"cluster_id": "c1", "canonical_title": "has ```fence``` inside"}]}
        body = ("<details>\r\n\r\n```json\r\n" + json.dumps(doc, indent=2).replace("\n", "\r\n") + "\r\n```\r\n</details>")
        self.assertEqual(extract_doc(body), doc)

    def test_extract_doc_returns_none_when_there_is_no_triage_block(self):
        self.assertIsNone(extract_doc(""))
        self.assertIsNone(extract_doc("```mermaid\nflowchart LR\n```"))
        self.assertIsNone(extract_doc(fence({"no": "clusters"})))

    def test_upgrade_legacy_builds_the_full_envelope(self):
        legacy = copy.deepcopy(LEGACY_SCORED)
        doc = upgrade_legacy(legacy)
        self.assertEqual(legacy, LEGACY_SCORED)  # input is not mutated
        self.assertEqual(doc["schema"], SCHEMA)
        self.assertEqual((doc["pr"], doc["head"]), (None, None))
        self.assertEqual(doc["generated_at"], LEGACY_SCORED["generated_at"])
        self.assertEqual(doc["totals"], LEGACY_SCORED["totals"])
        self.assertEqual(doc["reviewers"], {"bugbot": 1, "copilot": 1, "devin": 1})
        self.assertEqual(doc["priors"], {})
        self.assertEqual(doc["lanes"], {"agreed": ["c1"], "solo": ["c2"]})
        self.assertEqual((doc["deferred"], doc["timeline"], doc["orphaned_dispositions"]), ([], {}, []))
        c1, c2 = doc["clusters"]
        self.assertEqual((c1["lane"], c1["p_tag"], c1["validation"]), ("agreed", "P1", {"mode": "escalate"}))
        self.assertEqual((c2["lane"], c2["p_tag"], c2["validation"]), ("solo", "P3", {"mode": "prove_or_kill"}))
        self.assertEqual((c1["mechanisms_claimed"], c1["disposition_history"]), ([], []))
        self.assertEqual(c1["locations"], [
            {"file": "src/options.ts", "lines": [12, 14], "from": "bugbot-1"},
            {"file": "src/routes.ts", "lines": [88, 88], "from": "devin-1"},
        ])
        self.assertEqual(c1["members"], LEGACY_SCORED["clusters"][0]["members"])  # scored fields pass through
        self.assertNotIn("disposition", c1)

    def test_upgrade_legacy_tolerates_clusters_without_members(self):
        legacy = {"clusters": [{"cluster_id": "c1", "severity": "critical", "quorum": 3,
                                "reviewers": ["a", "b", "c"], "member_ids": ["a-1", "b-1", "c-1"]}]}
        doc = upgrade_legacy(legacy)
        c1 = doc["clusters"][0]
        self.assertEqual((c1["lane"], c1["p_tag"], c1["locations"]), ("agreed", "P1", []))
        self.assertEqual(doc["reviewers"], {})
        self.assertEqual(doc["lanes"], {"agreed": ["c1"], "solo": []})

    def test_upgrade_legacy_dedupes_locations_and_skips_members_without_a_file(self):
        legacy = {"clusters": [{
            "cluster_id": "c1", "severity": "minor", "quorum": 2, "reviewers": ["a", "b"],
            "members": [
                {"id": "a-1", "reviewer": "a", "file": "src/x.ts", "lines": [1, 2]},
                {"id": "b-1", "reviewer": "b", "file": "src/x.ts", "lines": [1, 2]},
                {"id": "b-2", "reviewer": "b", "file": None, "lines": None},
                {"id": "b-3", "reviewer": "b", "file": "src/y.ts", "lines": None},
            ]}]}
        c1 = upgrade_legacy(legacy)["clusters"][0]
        self.assertEqual(c1["locations"], [
            {"file": "src/x.ts", "lines": [1, 2], "from": "a-1"},
            {"file": "src/y.ts", "lines": [None, None], "from": "b-3"},
        ])

    def test_upgrade_legacy_keeps_fields_a_cluster_already_has(self):
        legacy = copy.deepcopy(LEGACY_SCORED)
        legacy["clusters"][1]["disposition"] = {"verdict": "invalid", "at": "t"}
        legacy["clusters"][1]["disposition_history"] = [{"verdict": "valid", "at": "t0"}]
        c2 = upgrade_legacy(legacy)["clusters"][1]
        self.assertEqual(c2["disposition"], {"verdict": "invalid", "at": "t"})
        self.assertEqual(c2["disposition_history"], [{"verdict": "valid", "at": "t0"}])

    def test_upgrade_legacy_leaves_a_triage_doc_unchanged(self):
        triage = load("triage.json")
        upgraded = upgrade_legacy(triage)
        self.assertEqual(upgraded, triage)
        upgraded["clusters"][0]["lane"] = "mutated"
        self.assertEqual(triage["clusters"][0]["lane"], "agreed")  # a copy, not an alias

    def test_upgraded_legacy_doc_is_gate_ready(self):
        doc = upgrade_legacy(LEGACY_SCORED)
        self.assertEqual(compute_gate(doc)["state"], "pending")
        doc = with_disposition(doc, "c2", verdict="invalid", note="n")
        self.assertEqual(compute_gate(doc)["blocking"], ["c1"])


if __name__ == "__main__":
    unittest.main()
