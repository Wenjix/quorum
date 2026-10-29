import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import copy
import json
import unittest

from quorum_merge import inherit, member_comment_ids

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# An old-format scored doc (no `schema`): the shape clusters.scored.json had
# before quorum/triage@1. Its member comment ids overlap the fixture's c1.
LEGACY_PRIOR = {
    "generated_at": "2026-06-12T00:00:00Z",
    "totals": {"findings": 2, "clusters": 1, "reviewer_denominator": 3, "gate_split": []},
    "clusters": [
        {
            "cluster_id": "c1",
            "member_ids": ["bugbot-1", "devin-1"],
            "canonical_title": "Pipe writer loses data when the pipe is full",
            "severity": "major",
            "primary_location": {"file": "src/a.ts", "start_line": 40, "end_line": 40},
            "quorum": 2,
            "reviewers": ["bugbot", "devin"],
            "members": [
                {"id": "bugbot-1", "reviewer": "bugbot", "file": "src/a.ts", "lines": [40, 40],
                 "url": "https://example.test/comment/1", "comment_id": 1001},
                {"id": "devin-1", "reviewer": "devin", "file": "src/b.ts", "lines": [30, 31],
                 "url": "https://example.test/comment/2", "comment_id": 1002},
            ],
        },
    ],
}


def load():
    return json.loads((FIXTURES / "triage.json").read_text())


def cluster(doc, cid):
    return next(c for c in doc["clusters"] if c["cluster_id"] == cid)


def disposition(at, verdict="valid", **extra):
    return {"verdict": verdict, "note": "n", "evidence": [], "by": "test", "at": at, **extra}


def with_dispositions(doc, **by_cluster):
    """Deep copy of `doc` with a latest disposition (and optional history) set per cluster id."""
    out = copy.deepcopy(doc)
    for cid, value in by_cluster.items():
        c = cluster(out, cid)
        if isinstance(value, tuple):
            c["disposition"], c["disposition_history"] = value
        else:
            c["disposition"] = value
    return out


def renumber(doc, old, new, comment_ids=None):
    """Deep copy of `doc` with cluster `old` renamed to `new` (lanes updated) and,
    when given, its members' comment ids replaced in member order."""
    out = copy.deepcopy(doc)
    c = cluster(out, old)
    c["cluster_id"] = new
    for ids in out["lanes"].values():
        ids[:] = [new if i == old else i for i in ids]
    for m, comment_id in zip(c["members"], comment_ids or []):
        m["comment_id"] = comment_id
    return out


def moved(doc, head="def5678"):
    out = copy.deepcopy(doc)
    out["head"] = head
    return out


class CarryTest(unittest.TestCase):
    def test_carries_disposition_by_comment_id_overlap(self):
        earlier = disposition("2026-10-29T10:01:00+00:00", "partly")
        latest = disposition("2026-10-29T10:05:00+00:00", "valid", commit="ba814e4")
        prior = with_dispositions(load(), c1=(latest, [earlier]))
        cluster(prior, "c1")["escalation"] = {"invariant": "Writes are all-or-nothing.", "siblings": []}
        # Re-clustering renamed c1 to c9; it keeps only comment 1001 of its two members.
        new = renumber(load(), "c1", "c9", [1001, 7777])
        prior_before, new_before = copy.deepcopy(prior), copy.deepcopy(new)

        out = inherit(new, prior, [])

        c9 = cluster(out, "c9")
        self.assertEqual(c9["disposition"], latest)
        self.assertEqual(c9["disposition_history"], [earlier])
        self.assertEqual(c9["escalation"], {"invariant": "Writes are all-or-nothing.", "siblings": []})
        self.assertNotIn("stale", c9["disposition"])
        for cid in ("c2", "c3", "c4"):
            self.assertNotIn("disposition", cluster(out, cid))
        self.assertEqual(out["orphaned_dispositions"], [])
        # Pure: neither input changes, and the result shares no structure with them.
        self.assertEqual(prior, prior_before)
        self.assertEqual(new, new_before)
        self.assertIsNot(out, new)
        self.assertIsNot(c9["disposition"], cluster(prior, "c1")["disposition"])
        self.assertIsNot(c9["disposition_history"], cluster(prior, "c1")["disposition_history"])

    def test_a_split_prior_cluster_passes_its_disposition_to_both_successors(self):
        prior = with_dispositions(load(), c1=disposition("2026-10-29T10:05:00+00:00"))
        new = renumber(load(), "c1", "c8", [1001, 8002])
        second = copy.deepcopy(cluster(new, "c8"))
        second["cluster_id"] = "c9"
        second["members"] = [{**second["members"][1], "comment_id": 1002}]
        new["clusters"].append(second)

        out = inherit(new, prior, [])

        self.assertEqual(cluster(out, "c8")["disposition"], cluster(prior, "c1")["disposition"])
        self.assertEqual(cluster(out, "c9")["disposition"], cluster(prior, "c1")["disposition"])
        self.assertEqual(out["orphaned_dispositions"], [])

    def test_newest_disposition_wins_on_double_match(self):
        old = disposition("2026-10-29T10:01:00+00:00", "invalid")
        old_history = disposition("2026-10-29T10:00:00+00:00", "deferred")
        new_ = disposition("2026-10-29T10:09:00+00:00", "valid", commit="ba814e4")
        new_history = disposition("2026-10-29T10:07:00+00:00", "partly")
        # Re-clustering merged c1 and c2: c9 holds comment 1001 (from c1) and 1003 (from c2).
        for older_first in (True, False):
            with self.subTest(older_first=older_first):
                prior = with_dispositions(load(), c1=(old, [old_history]), c2=(new_, [new_history]))
                if not older_first:
                    prior["clusters"].sort(key=lambda c: c["cluster_id"] != "c2")
                new = renumber(load(), "c1", "c9", [1001, 1003])
                new["clusters"] = [c for c in new["clusters"] if c["cluster_id"] != "c2"]

                out = inherit(new, prior, [])

                c9 = cluster(out, "c9")
                self.assertEqual(c9["disposition"], new_)
                # The winner's history as it stands, then the loser's history and latest.
                self.assertEqual(c9["disposition_history"], [new_history, old_history, old])
                self.assertEqual(out["orphaned_dispositions"], [])

    def test_history_of_three_matches_runs_oldest_loser_first(self):
        # Prior document order is c1, c2, c3, but by time c3 < c1 < c2, so neither
        # document order nor newest-first would give the expected history.
        h = {k: disposition(f"2026-10-29T10:{m}:00+00:00", v)
             for k, (m, v) in {"w_hist": ("08", "partly"), "c1_hist": ("05", "deferred"),
                               "c3_hist": ("02", "invalid"), "c1": ("06", "valid"),
                               "c2": ("09", "valid"), "c3": ("03", "invalid")}.items()}
        prior = with_dispositions(load(), c1=(h["c1"], [h["c1_hist"]]), c2=(h["c2"], [h["w_hist"]]),
                                  c3=(h["c3"], [h["c3_hist"]]))
        new = renumber(load(), "c1", "c9", [1001, 1003])
        c9 = cluster(new, "c9")
        c9["members"].append({**c9["members"][0], "id": "x-1", "comment_id": 1004})
        new["clusters"] = [c for c in new["clusters"] if c["cluster_id"] not in ("c2", "c3")]

        out = inherit(new, prior, [])

        self.assertEqual(cluster(out, "c9")["disposition"], h["c2"])
        self.assertEqual(cluster(out, "c9")["disposition_history"],
                         [h["w_hist"], h["c3_hist"], h["c3"], h["c1_hist"], h["c1"]])
        self.assertEqual(out["orphaned_dispositions"], [])

    def test_only_the_winners_escalation_is_carried(self):
        old = disposition("2026-10-29T10:01:00+00:00")
        new_ = disposition("2026-10-29T10:09:00+00:00")
        for winner_has_escalation in (True, False):
            with self.subTest(winner_has_escalation=winner_has_escalation):
                prior = with_dispositions(load(), c1=old, c2=new_)
                cluster(prior, "c1")["escalation"] = {"invariant": "from the loser"}
                if winner_has_escalation:
                    cluster(prior, "c2")["escalation"] = {"invariant": "from the winner"}
                new = renumber(load(), "c1", "c9", [1001, 1003])
                new["clusters"] = [c for c in new["clusters"] if c["cluster_id"] != "c2"]

                out = inherit(new, prior, [])

                c9 = cluster(out, "c9")
                if winner_has_escalation:
                    self.assertEqual(c9["escalation"], {"invariant": "from the winner"})
                else:
                    self.assertNotIn("escalation", c9)

    def test_prior_cluster_without_disposition_contributes_nothing(self):
        prior = load()  # no dispositions anywhere
        new = renumber(load(), "c1", "c9")

        out = inherit(new, prior, [])

        self.assertEqual(out, new)
        self.assertEqual(out["orphaned_dispositions"], [])

    def test_prior_none_returns_a_copy(self):
        new = load()
        out = inherit(new, None, None)
        self.assertEqual(out, new)
        self.assertIsNot(out, new)


class StaleTest(unittest.TestCase):
    def setUp(self):
        self.prior = with_dispositions(load(), c1=disposition("2026-10-29T10:05:00+00:00"))

    def test_marks_stale_when_head_moved_and_file_changed(self):
        # c1's locations are src/a.ts and src/b.ts; either changing makes it stale.
        for changed in (["src/a.ts"], ["README.md", "src/b.ts"]):
            with self.subTest(changed=changed):
                out = inherit(moved(load()), self.prior, changed)
                self.assertIs(cluster(out, "c1")["disposition"]["stale"], True)
        out = inherit(moved(load()), self.prior, ["other"])
        self.assertNotIn("stale", cluster(out, "c1")["disposition"])
        out = inherit(moved(load()), self.prior, [])
        self.assertNotIn("stale", cluster(out, "c1")["disposition"])

    def test_same_head_is_never_stale(self):
        out = inherit(load(), self.prior, ["src/a.ts"])
        self.assertNotIn("stale", cluster(out, "c1")["disposition"])
        out = inherit(load(), self.prior, None)
        self.assertNotIn("stale", cluster(out, "c1")["disposition"])

    def test_unknown_changed_files_marks_everything_stale(self):
        prior = with_dispositions(
            load(),
            c1=disposition("2026-10-29T10:05:00+00:00"),
            c2=disposition("2026-10-29T10:06:00+00:00", "invalid"),
            c3=disposition("2026-10-29T10:07:00+00:00", "deferred"),
        )
        out = inherit(moved(load()), prior, None)
        for cid in ("c1", "c2", "c3"):
            self.assertIs(cluster(out, cid)["disposition"]["stale"], True)
        self.assertNotIn("disposition", cluster(out, "c4"))

    def test_outdated_anchors_with_null_lines_still_match_on_file(self):
        new = moved(load())
        for loc in cluster(new, "c1")["locations"]:
            loc["lines"] = [None, None]
        out = inherit(new, self.prior, ["src/b.ts"])
        self.assertIs(cluster(out, "c1")["disposition"]["stale"], True)

    def test_an_already_stale_disposition_stays_stale(self):
        prior = with_dispositions(load(), c1=disposition("2026-10-29T10:05:00+00:00", stale=True))
        out = inherit(moved(load()), prior, [])
        self.assertIs(cluster(out, "c1")["disposition"]["stale"], True)


class OrphanTest(unittest.TestCase):
    def test_orphaned_prior_dispositions_are_kept(self):
        d1 = disposition("2026-10-29T10:05:00+00:00")
        d2 = disposition("2026-10-29T10:06:00+00:00", "invalid")
        prior = with_dispositions(load(), c1=d1, c2=d2)
        # c2 (comment 1003) is gone from the new run; a cluster with no disposition leaves nothing behind.
        new = load()
        new["clusters"] = [c for c in new["clusters"] if c["cluster_id"] not in ("c2", "c3")]

        out = inherit(new, prior, [])

        self.assertEqual(out["orphaned_dispositions"],
                         [{"cluster_id": "c2", "member_comment_ids": [1003], "disposition": d2}])
        self.assertEqual(cluster(out, "c1")["disposition"], d1)

    def test_orphan_member_comment_ids_are_sorted(self):
        d = disposition("2026-10-29T10:05:00+00:00")
        prior = with_dispositions(load(), c1=d)
        cluster(prior, "c1")["members"].reverse()
        new = load()
        new["clusters"] = [c for c in new["clusters"] if c["cluster_id"] != "c1"]

        out = inherit(new, prior, [])

        self.assertEqual(out["orphaned_dispositions"][0]["member_comment_ids"], [1001, 1002])


def orphan_entry(cluster_id, comment_ids, d, **extra):
    return {"cluster_id": cluster_id, "member_comment_ids": comment_ids, "disposition": d, **extra}


def without(doc, *cluster_ids):
    out = copy.deepcopy(doc)
    out["clusters"] = [c for c in out["clusters"] if c["cluster_id"] not in cluster_ids]
    return out


class EarlierOrphanTest(unittest.TestCase):
    """Orphans recorded by an earlier run stay in play on every later run."""

    def setUp(self):
        self.d1 = disposition("2026-10-29T10:05:00+00:00")
        self.d2 = disposition("2026-10-29T10:06:00+00:00", "invalid")
        # Run N: c2 vanished from the clustering, so its disposition became an orphan.
        run_n = inherit(without(load(), "c2"), with_dispositions(load(), c1=self.d1, c2=self.d2), [])
        self.assertEqual(run_n["orphaned_dispositions"], [orphan_entry("c2", [1003], self.d2)])
        self.run_n = run_n

    def test_an_orphan_from_run_n_survives_into_run_n_plus_1_unchanged(self):
        before = copy.deepcopy(self.run_n)

        # Same head, and again with the head moved and the changed files unknown:
        # the live cluster c1 goes stale in the second run, the unmatched orphan never does.
        for doc, c1_stale in ((without(load(), "c2"), None), (moved(without(load(), "c2")), True)):
            with self.subTest(c1_stale=c1_stale):
                out = inherit(doc, self.run_n, None)

                self.assertEqual(out["orphaned_dispositions"], before["orphaned_dispositions"])
                self.assertIsNot(out["orphaned_dispositions"][0], self.run_n["orphaned_dispositions"][0])
                self.assertIs(cluster(out, "c1")["disposition"].get("stale"), c1_stale)
        self.assertEqual(self.run_n, before)

    def test_an_orphan_reattaches_when_a_later_cluster_shares_a_comment_id(self):
        new = renumber(load(), "c2", "c7")  # c7 is back, still holding comment 1003

        out = inherit(new, self.run_n, [])

        c7 = cluster(out, "c7")
        self.assertEqual(c7["disposition"], self.d2)
        self.assertEqual(c7["disposition_history"], [])
        self.assertNotIn("escalation", c7)
        self.assertEqual(out["orphaned_dispositions"], [])

        # The stale rule applies to it like to any carried disposition (c2's file is src/c.ts).
        for changed, stale in ((["src/c.ts"], True), (["other"], None), (None, True)):
            with self.subTest(changed=changed):
                out = inherit(moved(new), self.run_n, changed)
                self.assertIs(cluster(out, "c7")["disposition"].get("stale"), stale)

    def test_an_orphan_competes_on_at_with_a_live_prior_cluster(self):
        live = disposition("2026-10-29T10:05:00+00:00", "partly")
        live_history = disposition("2026-10-29T10:04:00+00:00", "deferred")
        escalation = {"invariant": "from the orphan"}
        for orphan_at, orphan_wins in (("2026-10-29T10:09:00+00:00", True), ("2026-10-29T10:01:00+00:00", False)):
            with self.subTest(orphan_wins=orphan_wins):
                old = disposition(orphan_at, "invalid")
                old_history = disposition("2026-10-29T09:00:00+00:00", "deferred")
                prior = with_dispositions(load(), c1=(live, [live_history]))
                prior["orphaned_dispositions"] = [
                    orphan_entry("c5", [1003], old, disposition_history=[old_history], escalation=escalation)]
                # c9 holds comment 1001 (live prior c1) and 1003 (the orphan).
                new = without(renumber(load(), "c1", "c9", [1001, 1003]), "c2")

                out = inherit(new, prior, [])

                c9 = cluster(out, "c9")
                if orphan_wins:
                    self.assertEqual(c9["disposition"], old)
                    self.assertEqual(c9["disposition_history"], [old_history, live_history, live])
                    self.assertEqual(c9["escalation"], escalation)
                else:
                    self.assertEqual(c9["disposition"], live)
                    self.assertEqual(c9["disposition_history"], [live_history, old_history, old])
                    self.assertNotIn("escalation", c9)
                self.assertEqual(out["orphaned_dispositions"], [])

    def test_orphans_dedupe_on_sorted_comment_ids_keeping_the_first(self):
        first = orphan_entry("c2", [2001, 2002], self.d2)
        again = orphan_entry("c2-again", [2002, 2001], self.d1)
        other = orphan_entry("c5", [2000], self.d1)
        prior = load()
        prior["orphaned_dispositions"] = [first, again, other]

        out = inherit(without(load(), "c2"), prior, [])

        self.assertEqual(out["orphaned_dispositions"], [first, other])

    def test_earlier_orphans_come_first_then_this_runs_without_duplicates(self):
        d4 = disposition("2026-10-29T10:07:00+00:00", "partly")
        earlier = orphan_entry("c2-old", [1003], self.d2)
        # c2 shares comment 1003 with the earlier orphan, so it must not be listed twice; c4 is new.
        prior = with_dispositions(load(), c2=disposition("2026-10-29T10:08:00+00:00", "valid"), c4=d4)
        prior["orphaned_dispositions"] = [earlier]

        out = inherit(without(load(), "c2", "c4"), prior, [])

        self.assertEqual(out["orphaned_dispositions"],
                         [earlier, orphan_entry("c4", [1005, 1006], d4)])

    def test_the_orphan_list_is_stable_across_repeated_carries(self):
        doc = self.run_n
        for _ in range(3):
            doc = inherit(without(load(), "c2"), doc, [])
        self.assertEqual(doc["orphaned_dispositions"], [orphan_entry("c2", [1003], self.d2)])


class LegacyAndTimelineTest(unittest.TestCase):
    def test_legacy_prior_doc_carries_nothing(self):
        new = load()
        out = inherit(new, copy.deepcopy(LEGACY_PRIOR), ["src/a.ts"])

        self.assertEqual(out, new)
        self.assertFalse(any("disposition" in c for c in out["clusters"]))
        self.assertEqual(out["orphaned_dispositions"], [])

    def test_plan_at_is_carried_from_the_prior_timeline(self):
        prior = load()
        prior["timeline"] = {"plan_at": "2026-10-29T09:00:00+00:00",
                             "first_disposition_at": "2026-10-29T10:05:00+00:00"}
        out = inherit(load(), prior, [])
        self.assertEqual(out["timeline"], prior["timeline"])
        self.assertIsNot(out["timeline"], prior["timeline"])

        # A plan_at already on the new doc is kept.
        new = load()
        new["timeline"] = {"plan_at": "2026-10-29T11:00:00+00:00"}
        out = inherit(new, prior, [])
        self.assertEqual(out["timeline"], {"plan_at": "2026-10-29T11:00:00+00:00"})


class MemberCommentIdsTest(unittest.TestCase):
    def test_collects_ids_and_ignores_members_without_one(self):
        c = {"members": [{"comment_id": 5}, {"comment_id": None}, {"id": "x"}, {"comment_id": 3}]}
        self.assertEqual(member_comment_ids(c), {3, 5})
        self.assertEqual(member_comment_ids({}), set())


if __name__ == "__main__":
    unittest.main()
