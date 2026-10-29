import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fakes import FakeClient, temp_cwd
from quorum_doc import MARKER
from quorum_priors import MAX_AGE_DAYS, MIN_N, aggregate, collect_docs, default_path, load_fresh_priors, main
from quorum_render import minimal_doc

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
PULLS = ("GET", "repos/o/r/pulls")


def cluster(cid, members, verdict, overrides=None, **extra):
    """A cluster whose latest disposition is `verdict`; `members` is [(id, reviewer)]."""
    disposition = {"verdict": verdict, "note": "n", "evidence": [], "by": "agent",
                   "at": "2026-10-29T13:00:00+00:00"}
    if overrides:
        disposition["members"] = overrides
    return {"cluster_id": cid, "members": [{"id": m, "reviewer": r} for m, r in members],
            "disposition": disposition, **extra}


def solos(reviewer, verdict, count, start=1):
    """`count` single-member clusters from `reviewer`, all judged `verdict`."""
    return [cluster(f"{reviewer}-c{i}", [(f"{reviewer}-{i}", reviewer)], verdict)
            for i in range(start, start + count)]


def doc(*clusters, **extra):
    return {"schema": "quorum/triage@1", "clusters": list(clusters), **extra}


def ledger_body(document):
    return f"{MARKER}\n## Quorum\n\n```json\n{json.dumps(document)}\n```\n"


def comment(cid, document=None, association="OWNER", login="me", body=None):
    return {"id": cid, "body": body if body is not None else ledger_body(document),
            "author_association": association, "user": {"login": login}}


class AggregateTest(unittest.TestCase):
    def test_aggregate_uses_member_overrides(self):
        shared = cluster("c9", [("devin-3", "devin"), ("bugbot-3", "bugbot")], "valid",
                         overrides={"devin-3": "partly"})
        got = aggregate([doc(*solos("devin", "valid", 4), *solos("bugbot", "valid", 4), shared)])
        self.assertEqual(got["devin"], {"valid": 0.8, "partly": 0.2, "invalid": 0.0, "n": 5})
        # The override names devin only; bugbot keeps the cluster's verdict.
        self.assertEqual(got["bugbot"], {"valid": 1.0, "partly": 0.0, "invalid": 0.0, "n": 5})

    def test_reviewers_under_min_n_are_omitted(self):
        self.assertEqual(MIN_N, 5)
        got = aggregate([doc(*solos("devin", "valid", MIN_N), *solos("bugbot", "valid", MIN_N - 1))])
        self.assertEqual(list(got), ["devin"])

    def test_deferred_is_ignored(self):
        deferred = cluster("c9", [("devin-9", "devin"), ("bugbot-9", "bugbot")], "valid",
                           overrides={"devin-9": "deferred"})
        got = aggregate([doc(*solos("devin", "deferred", 5), *solos("bugbot", "valid", 4), deferred)])
        # devin: five deferred clusters and a deferred member verdict count for nothing.
        self.assertNotIn("devin", got)
        self.assertEqual(got["bugbot"]["n"], 5)

    def test_aggregate_pools_clusters_across_documents(self):
        got = aggregate([doc(*solos("devin", "valid", 3)), doc(*solos("devin", "invalid", 2, start=4))])
        self.assertEqual(got["devin"], {"valid": 0.6, "partly": 0.0, "invalid": 0.4, "n": 5})

    def test_fractions_are_rounded_to_three_places(self):
        got = aggregate([doc(*solos("devin", "valid", 1), *solos("devin", "partly", 2, start=2),
                             *solos("devin", "invalid", 4, start=4))])
        self.assertEqual(got["devin"], {"valid": 0.143, "partly": 0.286, "invalid": 0.571, "n": 7})

    def test_a_stale_disposition_still_counts(self):
        stale = solos("devin", "valid", MIN_N)
        for c in stale:
            c["disposition"]["stale"] = True
        self.assertEqual(aggregate([doc(*stale)])["devin"]["n"], MIN_N)

    def test_orphaned_dispositions_carry_no_reviewer_and_are_not_counted(self):
        orphan = {"cluster_id": "gone", "member_comment_ids": [1, 2],
                  "disposition": {"verdict": "valid", "note": "n", "evidence": []}}
        got = aggregate([doc(*solos("devin", "valid", MIN_N), orphaned_dispositions=[orphan] * 3)])
        self.assertEqual(got["devin"]["n"], MIN_N)

    def test_clusters_without_a_usable_judgement_are_skipped(self):
        undecided = {"cluster_id": "c0", "members": [{"id": "devin-0", "reviewer": "devin"}]}
        odd = cluster("c00", [("devin-00", "devin")], "maybe")
        # No reviewer on the member and none to read off its id: nobody to credit.
        anonymous = [cluster(f"a{i}", [(f"m{i}", None)], "valid") for i in range(MIN_N)]
        got = aggregate([doc(*solos("devin", "valid", MIN_N), undecided, odd, *anonymous)])
        self.assertEqual(got["devin"]["n"], MIN_N)
        self.assertEqual(list(got), ["devin"])

    def test_a_truncated_ledger_counts_like_the_full_one(self):
        # The minimal level of a posted ledger keeps member ids but drops each member's reviewer.
        full = json.loads((FIXTURES / "triage.ledger.json").read_text())
        minimal = minimal_doc(full)
        self.assertNotIn("reviewer", minimal["clusters"][0]["members"][0])
        got = aggregate([minimal] * 3)
        self.assertEqual(got, aggregate([full] * 3))
        self.assertEqual(got["bugbot"], {"valid": 0.5, "partly": 0.5, "invalid": 0.0, "n": 6})
        self.assertEqual(got["devin"], {"valid": 1.0, "partly": 0.0, "invalid": 0.0, "n": 6})

    def test_no_documents_no_priors(self):
        self.assertEqual(aggregate([]), {})


class LoadFreshPriorsTest(unittest.TestCase):
    REVIEWERS = {"bugbot": {"valid": 0.9, "partly": 0.1, "invalid": 0.0, "n": 10}}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "priors.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def aged(self, days):
        return {"generated_at": (NOW - datetime.timedelta(days=days)).isoformat(timespec="seconds"),
                "repo": "o/r", "reviewers": self.REVIEWERS}

    def test_load_fresh_priors_respects_age(self):
        self.assertEqual(MAX_AGE_DAYS, 7)
        for days, expected in ((0, self.REVIEWERS), (6, self.REVIEWERS), (7, self.REVIEWERS), (8, {})):
            with self.subTest(days=days):
                self.write(self.aged(days))
                self.assertEqual(load_fresh_priors(self.path, NOW), expected)

    def test_a_missing_or_unusable_file_yields_no_priors(self):
        self.assertEqual(load_fresh_priors(self.path, NOW), {})  # never written
        good = self.aged(1)
        cases = {
            "not json": "{nope",
            "not an object": "[1, 2]",
            "no generated_at": {"reviewers": self.REVIEWERS},
            "bad generated_at": {**good, "generated_at": "yesterday"},
            "naive generated_at": {**good, "generated_at": "2026-10-28T12:00:00"},
            "non-string generated_at": {**good, "generated_at": 1761652800},
            "no reviewers": {"generated_at": good["generated_at"]},
            "reviewers not a map": {**good, "reviewers": ["bugbot"]},
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.write(data)
                self.assertEqual(load_fresh_priors(self.path, NOW), {})


class DefaultPathTest(unittest.TestCase):
    """The cache lives at the repository's top level, wherever in it a run starts."""

    def test_outside_a_git_repository_it_is_in_the_working_directory(self):
        cwd = temp_cwd(self)
        self.assertEqual(default_path("Wenjix/quorum"),
                         os.path.join(cwd, ".quorum", "priors", "Wenjix-quorum.json"))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_inside_a_git_repository_it_is_at_the_top_level(self):
        top = temp_cwd(self, git=True)
        for cwd in (top, os.path.join(top, "scratch", "deeper")):
            with self.subTest(cwd=cwd):
                os.makedirs(cwd, exist_ok=True)
                os.chdir(cwd)
                self.assertEqual(default_path("o/r"), os.path.join(top, ".quorum", "priors", "o-r.json"))

    def test_without_git_it_falls_back_to_the_working_directory(self):
        cwd = temp_cwd(self)
        for failure in (FileNotFoundError("git"), PermissionError("git")):
            with self.subTest(failure=failure), mock.patch("quorum_priors.subprocess.run", side_effect=failure):
                self.assertEqual(default_path("o/r"), os.path.join(cwd, ".quorum", "priors", "o-r.json"))


class CollectDocsTest(unittest.TestCase):
    def test_collect_docs_reads_marker_comments_across_prs(self):
        d1, d2 = doc(*solos("devin", "valid", 2)), doc(*solos("bugbot", "invalid", 3))
        client = FakeClient({
            PULLS: [{"number": 1}, {"number": 2}, {"number": 3}, {"number": 4}, {"number": 5}, {"number": 6}],
            ("GET", "user"): {"login": "me"},
            ("GET", "repos/o/r/issues/1/comments"): [comment(10, d1)],
            # A forged ledger from an outsider is ignored; the insider's older one stands.
            ("GET", "repos/o/r/issues/2/comments"): [
                comment(20, d2, association="OWNER"),
                comment(21, doc(*solos("mallory", "valid", 9)), association="NONE", login="mallory")],
            ("GET", "repos/o/r/issues/3/comments"): [{"id": 30, "body": "just a chat", "user": {"login": "me"}}],
            ("GET", "repos/o/r/issues/4/comments"): [],
            # A marker comment whose JSON does not parse, and one that is not a document at all.
            ("GET", "repos/o/r/issues/5/comments"): [comment(50, body=f"{MARKER}\n```json\n{{broken\n```")],
            ("GET", "repos/o/r/issues/6/comments"): [comment(60, body=f"{MARKER}\nno json here")],
        })
        self.assertEqual(collect_docs(client, "o/r"), [d1, d2])
        method, path, body, params = client.calls[0]
        self.assertEqual((method, path, params), ("GET", "repos/o/r/pulls", {"state": "all", "paginate": True}))
        # The authenticated login is looked up once for the whole repository, not once per PR.
        self.assertEqual([c[1] for c in client.calls].count("user"), 1)

    def test_a_repository_without_pull_requests_has_no_documents(self):
        self.assertEqual(collect_docs(FakeClient({PULLS: []}), "o/r"), [])
        self.assertEqual(collect_docs(FakeClient(), "o/r"), [])


class MainTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name

    def client(self, documents):
        responses = {PULLS: [{"number": n} for n in range(1, len(documents) + 1)], ("GET", "user"): {"login": "me"}}
        for n, document in enumerate(documents, start=1):
            responses[("GET", f"repos/o/r/issues/{n}/comments")] = [comment(n * 10, document)]
        return FakeClient(responses)

    def run_main(self, argv, client):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv, client=client)
        return code, out.getvalue(), err.getvalue()

    def test_main_writes_priors_to_out_and_summarises_each_reviewer(self):
        out_path = os.path.join(self.dir, "nested", "priors.json")
        client = self.client([doc(*solos("devin", "valid", 4), *solos("bugbot", "partly", 5)),
                              doc(*solos("devin", "invalid", 1, start=5))])
        code, out, err = self.run_main(["o/r", "--out", out_path], client)
        self.assertEqual(code, 0, err)
        with open(out_path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["repo"], "o/r")
        self.assertEqual(written["reviewers"], {
            "devin": {"valid": 0.8, "partly": 0.0, "invalid": 0.2, "n": 5},
            "bugbot": {"valid": 0.0, "partly": 1.0, "invalid": 0.0, "n": 5},
        })
        generated = datetime.datetime.fromisoformat(written["generated_at"])
        self.assertLess(abs(datetime.datetime.now(UTC) - generated), datetime.timedelta(minutes=1))
        lines = out.splitlines()
        self.assertIn(out_path, lines[0])
        self.assertEqual([l for l in lines if l.startswith(("devin", "bugbot"))], [
            "bugbot: valid 0.00, partly 1.00, invalid 0.00 (n=5)",
            "devin: valid 0.80, partly 0.00, invalid 0.20 (n=5)",
        ])
        # What main writes is what the post-plan wiring reads back.
        self.assertEqual(load_fresh_priors(out_path, datetime.datetime.now(UTC)), written["reviewers"])

    def test_main_writes_the_default_path_in_the_working_directory(self):
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            code, out, err = self.run_main(["o/r"], self.client([doc(*solos("devin", "valid", MIN_N))]))
            self.assertEqual(code, 0, err)
            with open(os.path.join(self.dir, ".quorum", "priors", "o-r.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["reviewers"]["devin"]["n"], MIN_N)
        finally:
            os.chdir(cwd)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_main_from_a_subdirectory_writes_at_the_repository_top_level(self):
        top = temp_cwd(self, git=True)
        os.makedirs(os.path.join(top, "scratch"))
        os.chdir(os.path.join(top, "scratch"))
        code, out, err = self.run_main(["o/r"], self.client([doc(*solos("devin", "valid", MIN_N))]))
        self.assertEqual(code, 0, err)
        written = os.path.join(top, ".quorum", "priors", "o-r.json")
        self.assertIn(written, out.splitlines()[0])
        with open(written, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["reviewers"]["devin"]["n"], MIN_N)
        self.assertFalse(os.path.exists(os.path.join(top, "scratch", ".quorum")))

    def test_main_with_no_ledgers_still_writes_an_empty_map_and_says_so(self):
        out_path = os.path.join(self.dir, "priors.json")
        code, out, err = self.run_main(["o/r", "--out", out_path], FakeClient({PULLS: [{"number": 1}]}))
        self.assertEqual(code, 0, err)
        with open(out_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["reviewers"], {})
        self.assertIn("no Quorum ledgers found in o/r", out)

    def test_main_with_ledgers_but_too_few_judgements_says_so(self):
        out_path = os.path.join(self.dir, "priors.json")
        code, out, err = self.run_main(["o/r", "--out", out_path], self.client([doc(*solos("devin", "valid", 2))]))
        self.assertEqual(code, 0, err)
        with open(out_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["reviewers"], {})
        self.assertIn(f"no reviewer has {MIN_N} judged findings yet", out)

    def test_main_reports_a_failed_read_and_writes_nothing(self):
        out_path = os.path.join(self.dir, "priors.json")
        client = FakeClient({PULLS: RuntimeError("HTTP 404")})
        code, out, err = self.run_main(["o/r", "--out", out_path], client)
        self.assertEqual(code, 1)
        self.assertIn("error: HTTP 404", err)
        self.assertFalse(os.path.exists(out_path))

    def test_usage_errors_return_2(self):
        for argv in ([], ["o/r", "--bogus"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(argv, FakeClient())
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)


if __name__ == "__main__":
    unittest.main()
